"""ECAD schematic drawing-quality MCP tools — the geometric self-check ERC cannot do.

``ecad_run_erc`` is electrical: undriven rails, output-output shorts, floating pins. A schematic
can pass it and still be a mess a human would reject — wires routed through symbol bodies,
dangling wire stubs, labels on empty paper, symbols drawn on top of each other, or unresolved
bus routing. This module exposes ONE tool, ``ecad_lint_schematic``, which reports four geometric
and three bus-completeness categories with offending ids/coordinates/text so the agent can
self-check BEFORE ERC and handoff.

The geometry lives in services/ecad/sch_qa.py (pure functions over the doc dict); bus completeness
uses the compiled netlist. This module is the MCP surface: resolve the active schematic, run both
passes, and phrase the result so the next call is obvious.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature

from services.ecad import sch_qa

from .ecad_common import (
    OpError,
    _dumps,
    _err,
    _get_sch,
    netlist_mod,
)


def _point_on_polyline(pos: object, pts: object, tol: float = 1e-6) -> bool:
    if not isinstance(pos, (list, tuple)) or len(pos) != 2:
        return False
    if not isinstance(pts, list) or len(pts) < 2:
        return False
    px, py = float(pos[0]), float(pos[1])
    for a, b in zip(pts, pts[1:]):
        if not (isinstance(a, (list, tuple)) and len(a) == 2
                and isinstance(b, (list, tuple)) and len(b) == 2):
            continue
        ax, ay = float(a[0]), float(a[1])
        bx, by = float(b[0]), float(b[1])
        dx, dy = bx - ax, by - ay
        den = dx * dx + dy * dy
        t = 0.0 if den == 0.0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / den))
        qx, qy = ax + t * dx, ay + t * dy
        if (px - qx) ** 2 + (py - qy) ** 2 <= tol * tol:
            return True
    return False


def _bus_findings(doc: dict, netlist: dict) -> dict[str, list[dict]]:
    """Agent-actionable bus completeness checks, derived from compiled bus bindings."""
    out: dict[str, list[dict]] = {
        "unresolved_bus_entry": [],
        "unused_bus_label": [],
        "unnamed_bus": [],
    }
    buses = [b for b in (doc.get("buses") or []) if isinstance(b, dict)]
    by_id = {str(b.get("id")): b for b in buses if b.get("id")}
    by_name = {str(b.get("name")): b for b in buses if str(b.get("name") or "").strip()}
    compiled_buses = netlist.get("buses") or {}

    for bus in buses:
        if str(bus.get("name") or "").strip():
            continue
        out["unnamed_bus"].append({
            "kind": "unnamed_bus",
            "bus_id": bus.get("id"),
            "sheet": bus.get("sheet"),
            "message": (f"bus {bus.get('id', '?')} has no name — name it with "
                        "ecad_delete + ecad_bus(name, points), or ecad_edit_bus."),
        })

    warned_ids: set[str] = set()
    for warning in netlist.get("warnings") or []:
        text = str(warning)
        if not text.startswith("bus_entry_unresolved:"):
            continue
        entry_id = next((str(e.get("id")) for e in (doc.get("bus_entries") or [])
                         if isinstance(e, dict) and str(e.get("id")) in text), "")
        if entry_id:
            warned_ids.add(entry_id)
        out["unresolved_bus_entry"].append({
            "kind": "unresolved_bus_entry",
            "entry_id": entry_id or None,
            "message": text + " — set both bus and member with ecad_bus_entry.",
        })

    for entry in doc.get("bus_entries") or []:
        if not isinstance(entry, dict) or str(entry.get("id")) in warned_ids:
            continue
        bus_token = str(entry.get("bus") or "")
        member = str(entry.get("member") or "")
        bus = by_id.get(bus_token) or by_name.get(bus_token)
        bus_name = str((bus or {}).get("name") or bus_token)
        members = compiled_buses.get(bus_name)
        resolved = bool(bus and member and isinstance(members, list) and member in members)
        if resolved:
            continue
        out["unresolved_bus_entry"].append({
            "kind": "unresolved_bus_entry",
            "entry_id": entry.get("id"),
            "bus": bus_token or None,
            "member": member or None,
            "sheet": entry.get("sheet"),
            "pos": entry.get("pos"),
            "message": (f"bus entry {entry.get('id', '?')} does not resolve to a named bus member "
                        "— recreate it with ecad_bus_entry(x, y, bus='D[0..7]', member='D0')."),
        })

    net_info = netlist.get("net_info") or {}
    nets = netlist.get("nets") or {}
    for label in doc.get("labels") or []:
        if not isinstance(label, dict):
            continue
        name = str(label.get("text") or "")
        if name not in compiled_buses:
            continue
        on_bus = any(
            str(bus.get("name") or "") == name
            and str(bus.get("sheet") or "") == str(label.get("sheet") or "")
            and _point_on_polyline(label.get("pos"), bus.get("pts"))
            for bus in buses
        )
        if not on_bus:
            continue
        referenced = any(
            isinstance(info, dict) and info.get("bus") == name
            and bool(nets.get(net_name))
            for net_name, info in net_info.items()
        )
        if not referenced:
            out["unused_bus_label"].append({
                "kind": "unused_bus_label",
                "label_id": label.get("id"),
                "text": name,
                "sheet": label.get("sheet"),
                "pos": label.get("pos"),
                "members": compiled_buses.get(name) or [],
                "message": (f"bus label {name!r} has no member wire references — add a bus entry "
                            "for each used member and connect its wire-side endpoint."),
            })
    return out


def register_ecad_qa_tools(mcp: FastMCP) -> None:
    """ECAD schematic drawing-quality MCP tools: ecad_lint_schematic."""

    @mcp.tool()
    def ecad_lint_schematic(doc_id: str = "", max_per_kind: int = 20) -> str:
        """GEOMETRIC self-check of the active schematic — run this BEFORE ecad_run_erc.

        ERC checks electricity; this checks the DRAWING, and reports every offender by name:

          - ``symbol_overlap``      two symbol bodies drawn on top of each other (both refdes
                                    + the overlapping mm box)
          - ``dangling_wire``       a wire end touching no pin / wire / junction / label /
                                    power port / no-connect (wire id, which end, its xy, and
                                    the nearest connection point + how far it missed by)
          - ``wire_crosses_symbol`` a wire routed straight THROUGH a body (wire id, the refdes
                                    it crosses, entry/exit xy, mm inside the body)
          - ``floating_label``      a net label on no wire and no pin, so it names nothing
                                    (label text + xy)
          - ``unresolved_bus_entry`` an entry whose bus/member does not resolve
          - ``unused_bus_label``    a bus label with no member wire references
          - ``unnamed_bus``         a bus wire with no name

        Legitimate terminations on pins/labels/power/junction/no-connect are not reported, and
        nothing is compared across sheets. Bus checks use the compiled netlist's bus expansion;
        they do not guess connectivity from drawing proximity alone. A symbol whose body cannot
        be resolved from its graphics is skipped and named in
        ``checked.symbols_without_body`` rather than guessed at.

        ``clean: true`` means all seven checks found nothing. Every finding carries a ``message``
        naming the fix, so act on them one at a time and re-run.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, reason=e.code,
                        hint="Open a schematic first (this check is schematic-only).",
                        next_actions=["ecad_new_schematic('mydesign')",
                                      "ecad_status()  # see which docs are open"])
        try:
            report = sch_qa.check_schematic_geometry(
                session.doc, max_per_kind=int(max_per_kind or 0))
            geometry_line = sch_qa.verdict(report)
            netlist = netlist_mod.build_netlist(session.doc)
            bus_by_kind = _bus_findings(session.doc, netlist)
        except ValueError as e:
            return _err(str(e), reason="invalid_schematic",
                        hint="ecad_lint_schematic checks a SCHEMATIC's drawing geometry. For a "
                             "board use ecad_run_drc.",
                        next_actions=["ecad_run_drc()"])
        except Exception as e:  # noqa: BLE001 — a missing bus compile is not a clean drawing
            return _err(f"schematic lint compile failed: {type(e).__name__}: {e}",
                        reason="lint_compile_failed",
                        hint="Run ecad_status(); the bus checks require a compiled netlist.",
                        next_actions=["ecad_status()", "ecad_run_erc()"])

        cap = max(0, int(max_per_kind or 0))
        bus_total = 0
        bus_parts: list[str] = []
        for kind in ("unresolved_bus_entry", "unused_bus_label", "unnamed_bus"):
            items = bus_by_kind[kind]
            count = len(items)
            report["counts"][kind] = count
            bus_total += count
            if count:
                bus_parts.append(f"{count} {kind.replace('_', ' ')}")
            if cap and count > cap:
                report["truncated"][kind] = count - cap
                items = items[:cap]
            report["findings"].extend(items)
        report["total"] += bus_total
        report["clean"] = report["total"] == 0
        report["checked"]["bus_entries"] = len(session.doc.get("bus_entries") or [])
        if bus_total and geometry_line.startswith("Drawing is geometrically clean"):
            line = f"{bus_total} bus problem(s): " + ", ".join(bus_parts) + "."
        elif bus_total:
            line = geometry_line + " Plus " + ", ".join(bus_parts) + "."
        else:
            line = geometry_line
        if report["clean"]:
            next_actions = ["ecad_run_erc()  # drawing is clean — now check it electrically",
                            "ecad_status()"]
        else:
            next_actions = [f"FIX {f['kind']}: {f['message']}" for f in report["findings"][:3]]
            next_actions.append("ecad_lint_schematic()  # re-run after each fix")
        return _dumps({
            "ok": True,
            "doc_id": session.doc_id,
            "revision": session.revision,
            "clean": report["clean"],
            "total": report["total"],
            "counts": report["counts"],
            "findings": report["findings"],
            "truncated": report["truncated"],
            "checked": report["checked"],
            "verdict": line,
            "warnings": ([f"{n} more {k} finding(s) not shown — raise max_per_kind"
                          for k, n in report["truncated"].items()]
                         + ([f"no body geometry for {', '.join(report['checked']['symbols_without_body'])}"
                             f" — those symbols were skipped by the overlap/crossing checks"]
                            if report["checked"]["symbols_without_body"] else [])),
            "hint": line,
            "next_actions": next_actions,
        })
