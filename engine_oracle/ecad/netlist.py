"""Netlist builder — the single source of connectivity truth for AI-ECAD.

Executable form of ``docs/contracts/ecad/netlist.md`` (§1 data model, §2 connectivity algorithm).
Pure function of a ``SchematicDocument`` (``aicad.sch/1``): no side effects, never mutates the input
doc. Consumed by ERC (:mod:`engine.ecad.erc`), PCB net-binding, and SPICE export.

Connectivity is computed by a union-find over points snapped to a ``1e-6`` mm grid. The two hard
guarantees are:

* **Crossing without a junction does not connect** (KiCad behavior): two wires only union where they
  share an endpoint or where an explicit :class:`Junction` sits on both.
* **Determinism / byte-stability**: every set/dict iteration is sorted by id or coordinate, so the
  emitted netlist is identical (``json.dumps`` equal) across runs for a given schematic. Auto net
  names (``N$<n>``) are assigned in deterministic geometric order.

See :mod:`engine.ecad.schema` for the geometry helpers (``iter_placed_pins``, ``pin_world``,
``symbol_def``); this module never hand-rolls the pin transform.
"""

from __future__ import annotations

import re
from typing import Any

from engine.ecad import schema

# Grid snap tolerance (mm). Points within this distance collapse to one canonical key. The contract
# (sch_schema.md §5, netlist.md §2.1) fixes this at 1e-6 mm.
SNAP_TOL = 1e-6
_SNAP_INV = 1.0 / SNAP_TOL  # multiply-then-round is exact for the decade tolerances we use

Point = tuple[float, float]


# ── point snapping ──────────────────────────────────────────────────────────────

def _snap(p: Any) -> Point:
    """Round an ``[x, y]`` to the snap grid, returning a canonical hashable key.

    ``round(x * 1e6) / 1e6`` puts coincident-within-tolerance points onto identical float keys so
    they hash equal in the union-find. ``+ 0.0`` normalizes ``-0.0`` to ``0.0`` for byte stability.
    """
    x = round(float(p[0]) * _SNAP_INV) / _SNAP_INV + 0.0
    y = round(float(p[1]) * _SNAP_INV) / _SNAP_INV + 0.0
    return (x, y)


# ── union-find over snapped points ───────────────────────────────────────────────

class _UnionFind:
    """Disjoint-set over canonical (snapped) points. Roots are chosen deterministically.

    ``find`` path-compresses; ``union`` makes the lexicographically-smaller point the root so the
    canonical representative of a class is stable regardless of union order — a prerequisite for the
    byte-stable golden test.
    """

    def __init__(self) -> None:
        self._parent: dict[Point, Point] = {}

    def add(self, p: Point) -> None:
        if p not in self._parent:
            self._parent[p] = p

    def find(self, p: Point) -> Point:
        self.add(p)
        root = p
        while self._parent[root] != root:
            root = self._parent[root]
        # path compression
        while self._parent[p] != root:
            self._parent[p], p = root, self._parent[p]
        return root

    def union(self, a: Point, b: Point) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # deterministic root: smaller coordinate wins (lexicographic on the tuple)
        lo, hi = (ra, rb) if ra <= rb else (rb, ra)
        self._parent[hi] = lo

    def classes(self) -> dict[Point, list[Point]]:
        """Map canonical root -> sorted member points (deterministic)."""
        out: dict[Point, list[Point]] = {}
        for p in self._parent:
            out.setdefault(self.find(p), []).append(p)
        for root in out:
            out[root].sort()
        return out


# ── geometry predicates ──────────────────────────────────────────────────────────

def _on_segment(p: Point, a: Point, b: Point) -> bool:
    """True if point ``p`` lies on the closed segment ``a``-``b`` (within snap tolerance).

    Used to test whether a Junction sits on a wire's interior (T/4-way) so two wires can union there.
    Collinear (cross ~ 0) AND within the bounding interval (dot products), all with a tolerance
    scaled to the snap grid.
    """
    ax, ay = a
    bx, by = b
    px, py = p
    abx, aby = bx - ax, by - ay
    apx, apy = px - ax, py - ay
    seg_len = (abx * abx + aby * aby) ** 0.5
    if seg_len <= SNAP_TOL:
        return _snap(p) == _snap(a)
    # perpendicular distance via cross product, tolerance scaled to grid
    cross = abx * apy - aby * apx
    if abs(cross) > SNAP_TOL * seg_len:
        return False
    dot = apx * abx + apy * aby
    return -SNAP_TOL * seg_len <= dot <= seg_len * seg_len + SNAP_TOL * seg_len


# ── bus expansion (v1: parse + expand only; see follow-up note) ────────────────────

_BUS_RANGE_RE = re.compile(r"^(.*?)\[(\d+)\.\.(\d+)\]$")


def expand_bus_members(spec: Any) -> list[str]:
    """Expand a bus specification into ordered member net names.

    Accepts a range form ``"D[0..7]"`` -> ``["D0", ..., "D7"]`` (ascending or descending), or an
    explicit member list ``["D0", "D1", ...]`` (returned as-is). Unrecognized scalars become a
    single-member list.

    v1 scope: expansion only. Bus *routing* (wires tagged ``bus: true`` carrying members, and
    bus-entry stubs that tap a single member onto a wire) is a documented follow-up — members are
    parsed here so labels/UX can surface them, but they do not yet drive union-find merges.
    """
    if isinstance(spec, (list, tuple)):
        return [str(m) for m in spec]
    if not isinstance(spec, str):
        return [str(spec)]
    m = _BUS_RANGE_RE.match(spec)
    if not m:
        return [spec]
    base, lo_s, hi_s = m.group(1), int(m.group(2)), int(m.group(3))
    step = 1 if hi_s >= lo_s else -1
    return [f"{base}{i}" for i in range(lo_s, hi_s + step, step)]


# ── label name resolution ─────────────────────────────────────────────────────────

def _label_name(lb: dict) -> str:
    return str(lb.get("text", ""))


def _label_scope(lb: dict) -> str:
    return str(lb.get("scope", "local"))


# ── main entry point ───────────────────────────────────────────────────────────────

def build_netlist(sch: dict) -> dict:
    """Build the netlist (``docs/contracts/ecad/netlist.md`` §1) from a schematic document.

    Returns ``{"nets", "by_pin", "components", "warnings"}``. Pure: does not mutate ``sch``.
    """
    warnings: list[str] = []
    if not schema.is_schematic(sch):
        return {"nets": {}, "by_pin": {}, "components": [],
                "warnings": [f"not a schematic document (schema={sch.get('schema')!r})"]}

    uf = _UnionFind()

    # ── §2.2 Wires: union consecutive vertices; index segments per sheet for junction tests ──
    # wire_endpoints[(sheet, snapped_pt)] -> set of wire ids that touch that point (endpoint OR
    # interior vertex). Two wires sharing such a point union there.
    wire_pts: dict[str, list[Point]] = {}            # wire id -> snapped vertex list
    wire_sheet: dict[str, str] = {}
    pt_wires: dict[tuple[str, Point], set[str]] = {}  # (sheet, pt) -> wire ids meeting at that pt

    wires = sorted(sch.get("wires", []), key=lambda w: w.get("id", ""))
    for w in wires:
        wid = w.get("id", "")
        sheet = w.get("sheet", "")
        raw = w.get("pts", []) or []
        pts = [_snap(p) for p in raw]
        wire_pts[wid] = pts
        wire_sheet[wid] = sheet
        for p in pts:
            uf.add(p)
            pt_wires.setdefault((sheet, p), set()).add(wid)
        # union consecutive vertices of this wire
        for a, b in zip(pts, pts[1:]):
            uf.union(a, b)

    # Two distinct wires that share a vertex/endpoint are already connected: both touch the same
    # snapped Point key, which is a single node in the union-find. No extra work needed — that is
    # exactly why crossing *without* a shared vertex (or junction) does NOT connect.

    # ── §2.2(b) Junctions: a junction lying on >=2 wires unions those wires ──
    # Snap the junction point and union it with any wire vertex/segment it touches.
    junctions = sorted(sch.get("junctions", []), key=lambda j: j.get("id", ""))
    for j in junctions:
        sheet = j.get("sheet", "")
        jp = _snap(j.get("pos", [0, 0]))
        uf.add(jp)
        # find every wire (on the same sheet) whose polyline passes through jp, then union jp into
        # the nearest representative vertex of each so all such wires join at the junction.
        for wid in sorted(wire_pts):
            if wire_sheet.get(wid) != sheet:
                continue
            pts = wire_pts[wid]
            for a, b in zip(pts, pts[1:]):
                if _on_segment(jp, a, b):
                    # union jp into both segment endpoints; every wire passing through jp thus
                    # joins the same class. A vertex-coincident wire is already covered (a or b == jp
                    # is the same Point key). Crossing wires with NO junction never reach here.
                    uf.union(jp, a)
                    uf.union(jp, b)

    # ── §2.3 Pins: union each placed pin's world endpoint with coincident wire/pin points ──
    # pin_at[(sheet, pt)] -> list of (sym_id, pin_number); collect for pin-to-pin abutment + nets.
    pin_records: list[dict] = []   # one per placed pin, sorted later
    for sym, pin, world in schema.iter_placed_pins(sch):
        p = _snap(world)
        uf.add(p)
        sheet = sym.get("sheet", "")
        pin_records.append({
            "sym": sym.get("id", ""),
            "ref": sym.get("ref", ""),
            "pin": str(pin.get("number", "")),
            "name": str(pin.get("name", "")),
            "type": str(pin.get("type", "passive")),
            "sheet": sheet,
            "pt": p,
            "libid": sym.get("libid", ""),
        })
    # Pins are unioned implicitly: any two pins (or a pin and a wire vertex) at the same snapped
    # point already share that point in the union-find (direct abutment is legal). Nothing more to do
    # here — coincidence == the same Point key.

    def _attach_to_wires(pt: Point, sheet: str) -> None:
        """Union an attach point (label/power port ``pos``) into any wire it lies on (same sheet).

        Matches KiCad: a label/power port bonds to a wire when its position is anywhere ON the wire
        — at a vertex/endpoint OR on a segment interior — not only at vertices. A pin/vertex already
        sharing the snapped key is handled by point identity; this additionally captures the
        interior-of-segment case so a label dropped mid-wire still names that net.
        """
        for wid in sorted(wire_pts):
            if wire_sheet.get(wid) != sheet:
                continue
            seg_pts = wire_pts[wid]
            for a, b in zip(seg_pts, seg_pts[1:]):
                if _on_segment(pt, a, b):
                    uf.union(pt, a)
                    uf.union(pt, b)

    # ── §2.4 Labels: union coincident points, then union across equal name+scope ──
    labels = sorted(sch.get("labels", []),
                    key=lambda lb: (lb.get("id", ""),))
    # group labels by (name, scope-key) for the cross-merge. scope-key folds the sheet in for local.
    label_groups: dict[tuple[str, str], list[Point]] = {}
    label_points: list[dict] = []   # for ERC multiple_nets_label
    # hier_label_pts[(sheet, name)] -> the anchor point of equal-named hier labels on that sheet
    # (used by the hierarchy bridge below; a child's "CLK" hier label connects to the parent's "CLK"
    # sheet pin). All such labels on one sheet already merge via the per-sheet group below, so one
    # representative anchor per (sheet, name) is enough.
    hier_label_pts: dict[tuple[str, str], Point] = {}
    for lb in labels:
        name = _label_name(lb)
        if not name:
            continue
        scope = _label_scope(lb)
        sheet = lb.get("sheet", "")
        lp = _snap(lb.get("pos", [0, 0]))
        uf.add(lp)
        _attach_to_wires(lp, sheet)
        # local/hier labels merge only within the same sheet; global merges across all sheets.
        gkey = (name, "*global*") if scope == "global" else (name, f"sheet:{sheet}")
        label_groups.setdefault(gkey, []).append(lp)
        label_points.append({"name": name, "scope": scope, "sheet": sheet, "pt": lp})
        if scope == "hier":
            # first (deterministic, labels sorted by id) hier label of a (sheet, name) is the anchor
            hier_label_pts.setdefault((sheet, name), lp)
    for gkey in sorted(label_groups):
        anchors = sorted(label_groups[gkey])
        first = anchors[0]
        for other in anchors[1:]:
            uf.union(first, other)

    # ── §2.4(b) Hierarchy bridge: union each sheet-pin net with the child's equal-named hier label ──
    # A sheet symbol (on a parent sheet) instantiates a child sheet and exposes named sheet pins. A
    # sheet pin "CLK" of a symbol instantiating child C is the SAME net as the hier label "CLK" in C
    # (sch_schema.md §6 / netlist.md). The pin's pos attaches to the parent's wires/pins like a label
    # would; then we union it with the child's hier-label anchor of equal name. Deterministic: sheet
    # symbols and their pins are iterated in sorted order. Absent sheet_symbols => this loop is a
    # no-op and connectivity is byte-identical to the flat single-sheet behavior.
    sheet_pin_names: list[dict] = []   # naming candidates (treated at hier/local priority)
    sheet_symbols = sorted(sch.get("sheet_symbols", []), key=lambda h: h.get("id", ""))
    for hs in sheet_symbols:
        parent_sheet = hs.get("sheet", "")
        child = hs.get("child", "")
        for pn in sorted(hs.get("pins", []), key=lambda p: str(p.get("name", ""))):
            name = str(pn.get("name", ""))
            if not name:
                continue
            pp = _snap(pn.get("pos", [0, 0]))
            uf.add(pp)
            # the sheet pin lives on the PARENT sheet: bond it to parent wires/pins it touches.
            _attach_to_wires(pp, parent_sheet)
            sheet_pin_names.append({"name": name, "sheet": parent_sheet, "pt": pp})
            # bridge into the child sheet's equal-named hier label (if present).
            child_anchor = hier_label_pts.get((child, name))
            if child_anchor is not None:
                uf.union(pp, child_anchor)

    # ── §2.5 Power ports: union coincident points, then union all ports of equal net globally ──
    power = sorted(sch.get("power", []), key=lambda pw: pw.get("id", ""))
    power_groups: dict[str, list[Point]] = {}
    power_points: list[dict] = []
    for pw in power:
        net = str(pw.get("net", ""))
        if not net:
            continue
        pp = _snap(pw.get("pos", [0, 0]))
        uf.add(pp)
        _attach_to_wires(pp, pw.get("sheet", ""))
        power_groups.setdefault(net, []).append(pp)
        power_points.append({"net": net, "pt": pp})
    for net in sorted(power_groups):
        anchors = sorted(power_groups[net])
        first = anchors[0]
        for other in anchors[1:]:
            uf.union(first, other)

    # ── §2.6 Buses (v1: parse + expand only — does not drive union-find yet) ──
    for bus in sorted(sch.get("buses", []), key=lambda b: b.get("id", "")):
        members = expand_bus_members(bus.get("members", []))
        if members:
            warnings.append(
                f"bus {bus.get('id', '?')}: expanded {len(members)} member(s) "
                f"[{', '.join(members)}]; bus routing is a v1 follow-up (parse-only)")

    # ── §2.7 Name each class by priority, then emit nets / by_pin / components ──
    classes = uf.classes()  # root -> sorted member points

    # point -> class root, for fast membership of label/power/pin points.
    root_of: dict[Point, Point] = {}
    for root, members in classes.items():
        for m in members:
            root_of[m] = root

    # Gather naming candidates per class root.
    # power name: any power port whose point falls in the class.
    power_name_for: dict[Point, str] = {}
    for rec in sorted(power_points, key=lambda r: (r["pt"], r["net"])):
        root = root_of.get(rec["pt"])
        if root is None:
            continue
        # deterministic: first (sorted) power net seen for the class wins; record collisions.
        if root not in power_name_for:
            power_name_for[root] = rec["net"]

    # label names by scope precedence: global beats local.
    global_label_for: dict[Point, str] = {}
    local_label_for: dict[Point, str] = {}
    for rec in sorted(label_points, key=lambda r: (r["pt"], r["name"])):
        root = root_of.get(rec["pt"])
        if root is None:
            continue
        if rec["scope"] == "global":
            global_label_for.setdefault(root, rec["name"])
        else:  # local / hier treated as local for naming
            local_label_for.setdefault(root, rec["name"])

    # Sheet-pin names are naming candidates at the same (local/hier) priority as a hier label, so a
    # hierarchy-spanning net is named by its connected sheet-pin/hier-label name (above auto N$). A
    # hier label on the class, if any, already populated local_label_for; setdefault keeps that
    # higher-id-stable choice and only fills in the name for a class that has a sheet pin but no
    # local/hier label of its own.
    for rec in sorted(sheet_pin_names, key=lambda r: (r["pt"], r["name"])):
        root = root_of.get(rec["pt"])
        if root is None:
            continue
        local_label_for.setdefault(root, rec["name"])

    # Assign final names. Auto names N$<n> in deterministic geometric order: sort the unnamed class
    # roots by their canonical (smallest) point, then number sequentially.
    name_of_root: dict[Point, str] = {}
    auto_roots: list[Point] = []
    for root in sorted(classes):
        if root in power_name_for:
            name_of_root[root] = power_name_for[root]
        elif root in global_label_for:
            name_of_root[root] = global_label_for[root]
        elif root in local_label_for:
            name_of_root[root] = local_label_for[root]
        else:
            auto_roots.append(root)
    # auto_roots is already sorted (we iterated sorted(classes)); number them.
    for n, root in enumerate(auto_roots, start=1):
        name_of_root[root] = f"N${n}"

    # Detect duplicate explicit names colliding on distinct classes (two labels/ports same name but
    # different union classes) — surfaced as a build warning; ERC reports it as multiple_nets_label.
    name_to_roots: dict[str, set[Point]] = {}
    for root, nm in name_of_root.items():
        name_to_roots.setdefault(nm, set()).add(root)
    for nm in sorted(name_to_roots):
        if len(name_to_roots[nm]) > 1:
            warnings.append(
                f"net name {nm!r} resolves to {len(name_to_roots[nm])} distinct nets "
                f"(shorted-by-name without a wire); see ERC multiple_nets_label")

    # ── build nets + by_pin from pins (deterministic order) ──
    nets: dict[str, list[dict]] = {}
    by_pin: dict[str, str] = {}
    # pins per component, keyed by sym id.
    pins_by_sym: dict[str, dict[str, str]] = {}
    # pin NAME -> net for the *placed* unit, keyed by sym id, with the count of distinct pin
    # numbers carrying that name (so an ambiguous name — shared by two pins of one placed unit —
    # is NOT aliased). This feeds a number+name pins dict so subckt-backed parts (the op-amp, the
    # 555) whose sim.pin_order is by NAME resolve their nodes (Bug E3). iter_placed_pins already
    # filters to the placed unit, so the name->net map is unambiguous per unit (it never mixes the
    # op-amp's unit-1 IN+/IN- with unit-2's identically-named pins).
    pin_name_net_by_sym: dict[str, dict[str, str]] = {}
    pin_name_count_by_sym: dict[str, dict[str, int]] = {}

    pin_records.sort(key=lambda r: (r["ref"] or "~", r["sym"], r["pin"], r["pt"]))
    for rec in pin_records:
        pinref = f"{rec['sym']}.{rec['pin']}"
        root = root_of.get(rec["pt"])
        net = name_of_root.get(root, "") if root is not None else ""
        by_pin[pinref] = net
        pins_by_sym.setdefault(rec["sym"], {})[rec["pin"]] = net
        nm = rec.get("name") or ""
        # don't alias a name that equals its own number (R "1"/"2") or "~" (unnamed KiCad pin)
        if nm and nm != rec["pin"] and nm != "~":
            pin_name_net_by_sym.setdefault(rec["sym"], {})[nm] = net
            cnt = pin_name_count_by_sym.setdefault(rec["sym"], {})
            cnt[nm] = cnt.get(nm, 0) + 1
        if net:
            nets.setdefault(net, []).append({
                "sym": rec["sym"], "pin": rec["pin"], "ref": rec["ref"],
                "type": rec["type"], "xy": [rec["pt"][0], rec["pt"][1]],
            })

    # sort net membership deterministically and produce a sorted-key dict.
    nets_sorted: dict[str, list[dict]] = {}
    for net in sorted(nets):
        members = sorted(nets[net], key=lambda m: (m["ref"] or "~", m["sym"], m["pin"]))
        nets_sorted[net] = members

    # ── components list (one per placed symbol, deterministic by ref then id) ──
    components: list[dict] = []
    for sym in sorted(sch.get("symbols", []), key=lambda s: (s.get("ref", "~"), s.get("id", ""))):
        sid = sym.get("id", "")
        # Number-keyed pins, plus NAME-keyed aliases for the placed unit (so a subckt-backed part
        # whose sim.pin_order is by name — e.g. the op-amp's IN+/IN-/OUT — resolves; Bug E3). Only
        # an UNAMBIGUOUS name (exactly one pin of the placed unit carries it) is aliased, and a name
        # never shadows an existing number key.
        sym_pins = dict(sorted(pins_by_sym.get(sid, {}).items()))
        name_nets = pin_name_net_by_sym.get(sid, {})
        name_counts = pin_name_count_by_sym.get(sid, {})
        for nm, net in name_nets.items():
            if name_counts.get(nm, 0) == 1 and nm not in sym_pins:
                sym_pins[nm] = net
        comp = {
            "ref": sym.get("ref", ""),
            "libid": sym.get("libid", ""),
            "value": sym.get("value", ""),
            "footprint": sym.get("footprint", ""),
            "sym": sid,
            "pins": sym_pins,
            "fields": dict(sorted((sym.get("fields", {}) or {}).items())),
            "dnp": bool(sym.get("dnp", False)),
        }
        # Surface a source's small-signal AC magnitude (carried in sim_params by
        # build_design) so the SPICE netlister can give an AC sweep a stimulus
        # (Bug E-1 — otherwise an AC source built from a schematic has no AC drive).
        _sp = sym.get("sim_params") or {}
        if _sp.get("ac_mag") is not None:
            comp["ac_mag"] = _sp["ac_mag"]
        if _sp.get("ac_phase") is not None:
            comp["ac_phase"] = _sp["ac_phase"]
        # Carry per-instance engineering-metadata overrides (T-FIN8) so sync can put
        # them on the board footprint and boardphys/status see them (metadata.py).
        if isinstance(_sp.get("eng"), dict):
            comp["eng_overrides"] = _sp["eng"]
        components.append(comp)

    by_pin_sorted = dict(sorted(by_pin.items()))

    return {
        "nets": nets_sorted,
        "by_pin": by_pin_sorted,
        "components": components,
        "warnings": sorted(warnings),
    }


if __name__ == "__main__":  # pragma: no cover - smoke
    import json
    import sys
    doc = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else schema.new_schematic()
    print(json.dumps(build_netlist(doc), indent=2, sort_keys=True))
