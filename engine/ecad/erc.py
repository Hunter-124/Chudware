"""Electrical Rules Check (ERC) — advisory connectivity audit over a built netlist.

Executable form of ``docs/contracts/ecad/netlist.md`` §3 (rule catalog). ``run_erc(sch, netlist)``
returns a deterministic list of :data:`Violation` dicts; it never raises on a "bad" design and never
mutates its inputs. ERC is **advisory** — it surfaces issues in the RPC response / AI feedback block
but never blocks an op.

Determinism mirrors :mod:`engine.ecad.netlist`: every iteration is sorted (by net name, pin ref, or
coordinate) so the violation list is byte-stable for a given schematic.
"""

from __future__ import annotations

from typing import Any

from engine.ecad import schema

# Violation = {"rule":str, "severity":"error|warning", "net":str|None,
#              "pins":[pinref], "xy":[x,y]|None, "message":str}


# ── pin-type compatibility matrix (netlist.md §3) ────────────────────────────────
#
# Symmetric driver/sink compatibility between two pins sharing a net. Each cell is the worst
# severity of connecting those two pin types directly: ``None`` (fine), ``"warning"``, or
# ``"error"``. ``unspecified`` and ``free`` never raise (entire row/column is ``None``), per the
# contract. This table is the single source of truth for pairwise pin checks; the net-level rules
# (conflicting_drivers, power_not_driven, no_driver) below are derived aggregate checks.
#
# Driver capability summary used by the aggregate rules:
#   * "drives" (a source of signal/power): output, power_out, bidirectional, tri_state,
#     open_collector, open_emitter, passive (passive is a benign source — R/L/C).
#   * "sinks" (needs to be driven): input, power_in.
#   * power source: power_out (and power ports, handled via net naming in netlist).

PIN_TYPES = sorted(schema.PIN_TYPES)

# Pairwise matrix. Only the "interesting" pairs are listed; everything else (and any pair touching
# unspecified/free) is None = compatible. Stored as a frozenset-keyed dict so it is symmetric by
# construction: lookup canonicalizes the pair to a sorted tuple.
_PAIR: dict[tuple[str, str], str] = {}


def _set_pair(a: str, b: str, sev: str) -> None:
    key = (a, b) if a <= b else (b, a)
    _PAIR[key] = sev


# Hard output-output short: two strong drivers fighting.
_set_pair("output", "output", "error")
_set_pair("output", "power_out", "error")
_set_pair("power_out", "power_out", "error")
# A strong push-pull output driving a bidirectional pin is questionable (could fight) — warning.
_set_pair("output", "bidirectional", "warning")
# Everything else (including power_out -> power_in, the intended supply path, and any pair touching
# unspecified/free) is omitted and therefore treated as compatible (None) by pair_severity().


def pair_severity(a: str, b: str) -> str | None:
    """Worst severity of directly connecting pin types ``a`` and ``b`` (symmetric).

    Returns ``None`` when compatible. ``unspecified``/``free`` are always compatible.
    """
    if a in ("unspecified", "free") or b in ("unspecified", "free"):
        return None
    key = (a, b) if a <= b else (b, a)
    sev = _PAIR.get(key)
    return sev or None


# pin classes used by the net-level aggregate rules
_DRIVERS = {"output", "power_out", "bidirectional", "tri_state",
            "open_collector", "open_emitter", "passive"}
_STRONG_DRIVERS = {"output", "power_out"}
_SINKS = {"input", "power_in"}
_POWER_SOURCES = {"power_out"}


def _violation(rule: str, severity: str, net: str | None, pins: list[str],
               xy: Any, message: str) -> dict:
    return {"rule": rule, "severity": severity, "net": net,
            "pins": sorted(pins), "xy": list(xy) if xy is not None else None,
            "message": message}


def run_erc(sch: dict, netlist: dict) -> list[dict]:
    """Run the ERC rule catalog (``netlist.md`` §3) over a schematic + its netlist.

    Pure + advisory: returns a deterministic, sorted list of violations; never raises on a bad
    design, never mutates inputs.
    """
    out: list[dict] = []
    if not schema.is_schematic(sch):
        return out

    nets: dict[str, list[dict]] = netlist.get("nets", {})
    by_pin: dict[str, str] = netlist.get("by_pin", {})

    # ── index no-connect flags by the pin/point they cover ──
    # A NoConnect marks a pin intentionally open. Match it to placed pins by coincident world point.
    nc_points: list[tuple[float, float]] = []
    for nc in sch.get("no_connect", []):
        pos = nc.get("pos")
        if pos and len(pos) == 2:
            nc_points.append((round(float(pos[0]) * 1e6) / 1e6 + 0.0,
                              round(float(pos[1]) * 1e6) / 1e6 + 0.0))
    nc_set = set(nc_points)

    # map pinref -> (type, xy, ref) using the placed-pin geometry (single source of truth)
    pin_info: dict[str, dict] = {}
    pin_is_nc: dict[str, bool] = {}
    for sym, pin, world in schema.iter_placed_pins(sch):
        pinref = f"{sym.get('id', '')}.{pin.get('number', '')}"
        key = (round(float(world[0]) * 1e6) / 1e6 + 0.0,
               round(float(world[1]) * 1e6) / 1e6 + 0.0)
        pin_info[pinref] = {
            "type": str(pin.get("type", "passive")),
            "xy": [key[0], key[1]],
            "ref": sym.get("ref", ""),
            "name": str(pin.get("name", "")),
        }
        pin_is_nc[pinref] = key in nc_set

    # ── per-pin rules: pin_not_connected / no_connect_connected / floating_net_single_pin ──
    for pinref in sorted(by_pin):
        net = by_pin[pinref]
        info = pin_info.get(pinref, {})
        ptype = info.get("type", "passive")
        xy = info.get("xy")
        # size of the net this pin lives on (count of pin members)
        net_size = len(nets.get(net, [])) if net else 0
        is_nc = pin_is_nc.get(pinref, False)

        if (net == "" or net_size <= 1) and ptype != "no_connect":
            if not is_nc:
                out.append(_violation(
                    "pin_not_connected", "warning", net or None, [pinref], xy,
                    f"pin {pinref} ({ptype}) is not connected to any net"))
                # floating_net_single_pin is the same condition framed at net granularity; emit it
                # for a net that has exactly one pin (a wiring slip), distinct from a truly floating
                # (net=="") pin.
                if net and net_size == 1:
                    out.append(_violation(
                        "floating_net_single_pin", "warning", net, [pinref], xy,
                        f"net {net} has a single pin ({pinref}) — likely a wiring slip"))
        if is_nc and net and net_size > 1:
            out.append(_violation(
                "no_connect_connected", "error", net, [pinref], xy,
                f"pin {pinref} is flagged no-connect but is wired into net {net} "
                f"(size {net_size})"))

    # ── per-net aggregate rules ──
    for net in sorted(nets):
        members = nets[net]
        types = [str(m.get("type", "passive")) for m in members]
        pinrefs = [f"{m.get('sym', '')}.{m.get('pin', '')}" for m in members]
        net_xy = members[0].get("xy") if members else None

        strong = [pr for pr, t in zip(pinrefs, types) if t in _STRONG_DRIVERS]
        drivers = [t for t in types if t in _DRIVERS]
        sinks = [t for t in types if t in _SINKS]
        has_power_source = any(t in _POWER_SOURCES for t in types)
        # a net named like a power rail and driven by a power *port* is sourced even with no
        # power_out pin; the netlist names such nets after the port, and power ports carry no pin.
        port_powered = _net_has_power_port(sch, net)

        # conflicting_drivers: >=2 strong drivers (output/power_out) on one net
        if len(strong) >= 2:
            out.append(_violation(
                "conflicting_drivers", "error", net, strong, net_xy,
                f"net {net} has {len(strong)} strong drivers "
                f"({', '.join(sorted(strong))}) — output-output short"))

        # pairwise matrix scan (catches output<->output etc. with precise pin pairs; deduped by the
        # aggregate rule above for the exact conflicting_drivers case, so only emit non-error here)
        for i in range(len(types)):
            for k in range(i + 1, len(types)):
                sev = pair_severity(types[i], types[k])
                if sev == "warning":
                    out.append(_violation(
                        "pin_type_conflict", "warning", net,
                        [pinrefs[i], pinrefs[k]], net_xy,
                        f"pins {pinrefs[i]} ({types[i]}) and {pinrefs[k]} ({types[k]}) "
                        f"are a questionable combination on net {net}"))

        # power_not_driven: a power_in pin with no power_out / power port on the net
        if any(t == "power_in" for t in types) and not has_power_source and not port_powered:
            pin_in = [pr for pr, t in zip(pinrefs, types) if t == "power_in"]
            out.append(_violation(
                "power_not_driven", "error", net, pin_in, net_xy,
                f"net {net} has power_in pin(s) but no power_out or power port to drive it"))

        # no_driver: net has only input pins (no output/passive/power source of any kind)
        if members and not drivers and not port_powered and len(sinks) == len(types) and sinks:
            out.append(_violation(
                "no_driver", "warning", net, pinrefs, net_xy,
                f"net {net} has only sink pins ({', '.join(sorted(set(types)))}) — no driver"))

    # ── symbol-level rules: unannotated_ref / duplicate_ref / missing_footprint ──
    refs_seen: dict[str, str] = {}
    for sym in sorted(sch.get("symbols", []), key=lambda s: (s.get("ref", "~"), s.get("id", ""))):
        ref = str(sym.get("ref", ""))
        sid = sym.get("id", "")
        xy = sym.get("pos")
        if ref.endswith("?") or ref == "":
            out.append(_violation(
                "unannotated_ref", "warning", None, [sid], xy,
                f"symbol {sid} has an unannotated ref {ref!r}"))
        else:
            if ref in refs_seen:
                out.append(_violation(
                    "duplicate_ref", "error", None, [sid, refs_seen[ref]], xy,
                    f"ref {ref!r} is used by two symbols ({refs_seen[ref]} and {sid})"))
            else:
                refs_seen[ref] = sid
        # missing_footprint: on_board symbol with empty footprint (skip power symbols — virtual)
        sdef = schema.symbol_def(sch, sym.get("libid", "")) or {}
        is_power_sym = bool(sdef.get("power", False))
        if sym.get("on_board", True) and not sym.get("footprint", "") and not is_power_sym:
            out.append(_violation(
                "missing_footprint", "warning", None, [sid], xy,
                f"on-board symbol {sid} ({ref}) has no footprint assigned"))

    # ── multiple_nets_label: a label coincident with two different union classes ──
    # A label whose name resolves (via netlist naming) to more than one distinct net means the same
    # label text was placed on geometrically-separate nets. We detect by grouping labels by name and
    # checking the nets their coincident pins land on.
    out.extend(_multiple_nets_label(sch, netlist))

    # ── hierarchy: a sheet pin with no matching child hier label (or vice versa) ──
    out.extend(_hier_pin_label_mismatch(sch))

    # deterministic final ordering
    out.sort(key=lambda v: (v["rule"], v["net"] or "", tuple(v["pins"]),
                            tuple(v["xy"]) if v["xy"] else ()))
    return out


def _net_has_power_port(sch: dict, net: str) -> bool:
    """True if a power port with ``net == net`` exists (a power source for that named net)."""
    for pw in sch.get("power", []):
        if str(pw.get("net", "")) == net:
            return True
    return False


def _hier_pin_label_mismatch(sch: dict) -> list[dict]:
    """Hierarchy ERC: a sheet pin "X" on a symbol instantiating child C must have a matching
    ``hier`` label "X" in sheet C, and vice versa (a dangling cross-sheet connection point).

    Two warnings:
      * ``hier_pin_no_label`` - a sheet pin with no equal-named hier label in the child sheet.
      * ``hier_label_no_pin`` - a child-sheet hier label with no equal-named sheet pin on any symbol
        instantiating that sheet.
    Pure + deterministic (sorted). Absent sheet_symbols => no violations.
    """
    out: list[dict] = []
    symbols = sch.get("sheet_symbols", [])
    if not symbols:
        return out
    # hier labels in each sheet: sheet id -> set of names
    hier_by_sheet: dict[str, set[str]] = {}
    for lb in sch.get("labels", []):
        if str(lb.get("scope", "")) == "hier":
            nm = str(lb.get("text", ""))
            if nm:
                hier_by_sheet.setdefault(str(lb.get("sheet", "")), set()).add(nm)
    # which children are instantiated, and the pin names exposed for each child
    pins_for_child: dict[str, set[str]] = {}
    for hs in symbols:
        child = str(hs.get("child", ""))
        for pn in hs.get("pins", []):
            if isinstance(pn, dict):
                nm = str(pn.get("name", ""))
                if nm:
                    pins_for_child.setdefault(child, set()).add(nm)

    for hs in sorted(symbols, key=lambda h: h.get("id", "")):
        hid = hs.get("id", "")
        child = str(hs.get("child", ""))
        child_labels = hier_by_sheet.get(child, set())
        pos = hs.get("pos")
        for pn in sorted(hs.get("pins", []), key=lambda p: str(p.get("name", ""))):
            if not isinstance(pn, dict):
                continue
            nm = str(pn.get("name", ""))
            if nm and nm not in child_labels:
                out.append(_violation(
                    "hier_pin_no_label", "warning", None, [f"{hid}.{nm}"],
                    pn.get("pos") or pos,
                    f"sheet pin {nm!r} on {hid} has no matching hier label in child sheet "
                    f"{child!r}"))

    # vice versa: a hier label in an instantiated child with no exposing sheet pin
    for child in sorted(hier_by_sheet):
        if child not in pins_for_child:
            continue  # sheet not instantiated by any symbol -> not a hierarchy mismatch
        exposed = pins_for_child.get(child, set())
        for nm in sorted(hier_by_sheet[child]):
            if nm not in exposed:
                out.append(_violation(
                    "hier_label_no_pin", "warning", None, [f"{child}:{nm}"], None,
                    f"hier label {nm!r} in sheet {child!r} is not exposed as a sheet pin on any "
                    f"symbol instantiating it"))
    return out


def _multiple_nets_label(sch: dict, netlist: dict) -> list[dict]:
    """Detect a label name that ended up naming >1 distinct net (a label-only short)."""
    out: list[dict] = []
    nets: dict[str, list[dict]] = netlist.get("nets", {})
    # if a label text equals a net name AND another net also carries that text-as-name, it's a split.
    # Group label texts; cross-reference against the netlist's distinct net names is the simplest
    # deterministic proxy (the builder also warns on this).
    label_names: dict[str, list[dict]] = {}
    for lb in sch.get("labels", []):
        name = str(lb.get("text", ""))
        if name:
            label_names.setdefault(name, []).append(lb)

    # map each label to the net its coincident point resolves to (via by_pin proximity is hard
    # without the UF; we rely on the fact that a correctly-merged label yields exactly one net of
    # its name). If the builder produced two nets whose *names* collide, that surfaces here.
    name_counts: dict[str, set[str]] = {}
    for net_name in nets:
        # auto names never collide with labels
        if net_name.startswith("N$"):
            continue
        name_counts.setdefault(net_name, set()).add(net_name)

    for name in sorted(label_names):
        # a label that names nothing in the netlist (because its two placements landed on two
        # different classes that then got distinct names) is the failure case. When a single label
        # merges fine, the net is named after it (count==1). We flag the degenerate case where a
        # label text exists but no net carries it AND there is more than one label of that text on
        # distinct points (so it should have merged but didn't share geometry).
        lbs = label_names[name]
        if name not in nets and len(lbs) >= 2:
            pts = {(round(float(l.get("pos", [0, 0])[0]) * 1e6) / 1e6,
                    round(float(l.get("pos", [0, 0])[1]) * 1e6) / 1e6) for l in lbs}
            if len(pts) >= 2:
                xy = lbs[0].get("pos")
                out.append(_violation(
                    "multiple_nets_label", "error", name, [], xy,
                    f"label {name!r} is placed on {len(pts)} disconnected points but no net "
                    f"carries it — coincident with multiple union classes"))
    return out
