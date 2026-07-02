"""Unified BOM roll-up: made/bought bill of materials from an assembly doc + linked ECAD board.

Walk an ``aicad.asm/1`` assembly document tree and any linked ECAD board, emit one
:class:`BomLine` per distinct part, and classify each as **made** (produced from our geometry
+ a cost model, filled by T-B3) or **bought** (resolved to a priced ``VendorPart`` via the
provider chain). :func:`rollup` produces the unclassified-by-price BOM; :func:`resolve_prices`
folds provider pricing into the bought lines.

Contract: ``contracts/sourcing_schema.md`` — ``aicad.sourcing/1`` §3 (BomLine) is the canonical
line shape and is honored field-for-field. Two OPTIONAL fields are added additively (``mpn`` and
``description``); §0 permits new optional fields with no version bump, and every §3-shaped
consumer that reads only the frozen fields is unaffected because both default to ``None``/``""``.

Degradation (contract §0): a bought line whose manufacturer part number cannot be priced (no
credentials, no results — :meth:`ProviderChain.fetch` returns ``None``) keeps ``unit_cost`` /
``ext_cost`` at ``None`` (price *unknown*), never a fabricated number. The roll-up performs no
network call itself; the provider chain is the only network seam and it fails closed.

The mechanical assembly model (``aicad.asm/1``) is owned by the C++ core; :func:`rollup` consumes
the document-tree JSON it returns (``doc_tree``). Both the assembly loader and the board loader
are injectable so the roll-up logic is exercised fully offline (no DLL, no network) in CI.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

# Assembly-component source extensions that mark a component as a linked PCB: such a component is
# expanded into its electronic BOM (one bought line per grouped footprint) rather than treated as
# a single mechanical part.
_BOARD_EXTS = {".ecad", ".ecad+", ".kicad_pcb"}


@dataclass
class BomLine:
    """One line of a priced bill of materials (``aicad.sourcing/1`` §3).

    Exactly one of ``vendor_part`` (``bought``) / ``cost_model`` (``made``) is non-null, matching
    ``kind``. ``unit_cost`` for a bought line is resolved against ``vendor_part.price_breaks`` for
    ``quantity`` (falling back to ``unit_price``); an unpriced line keeps ``unit_cost``/``ext_cost``
    null rather than guessing.
    """

    line_id: str
    kind: str                                  # "made" | "bought"
    quantity: int = 1
    unit: str = "ea"
    part_ref: Optional[str] = None             # bought: "<supplier>::<part_number>" (VendorPart.key)
    vendor_part: Optional[dict] = None         # bought: resolved §1 record; else None
    cost_model: Optional[dict] = None          # made: cost-model result (T-B3); else None
    unit_cost: Optional[float] = None          # cost of one unit at `quantity`; None if unpriced
    ext_cost: Optional[float] = None           # unit_cost * quantity; None if unpriced
    currency: str = "USD"                      # ISO-4217 for unit_cost/ext_cost
    # --- Additive OPTIONAL fields (contract §0: new optional fields need no version bump). ---
    mpn: Optional[str] = None                  # manufacturer part number driving provider lookup
    description: str = ""                       # human-readable line description (order summary)


# --- Component field readers (tolerant of the aicad.asm/1 component shape) --------------------

def _component_source_path(comp: dict) -> str:
    src = comp.get("source")
    if isinstance(src, dict):
        path = src.get("path")
    else:
        path = comp.get("source_path") or comp.get("path")
    return str(path) if path else ""


def _component_mpn(comp: dict) -> Optional[str]:
    for k in ("mpn", "part_number", "vendor_part_number"):
        v = comp.get(k)
        if v:
            return str(v)
    specs = comp.get("specs")
    if isinstance(specs, dict):
        for k in ("mpn", "part_number"):
            v = specs.get(k)
            if v:
                return str(v)
    return None


def _component_supplier(comp: dict) -> Optional[str]:
    for k in ("supplier", "vendor_supplier"):
        v = comp.get(k)
        if v:
            return str(v)
    return None


def _component_description(comp: dict) -> str:
    for k in ("description", "name"):
        v = comp.get(k)
        if v:
            return str(v)
    return _component_source_path(comp) or ""


def _is_board(source_path: str) -> bool:
    if not source_path:
        return False
    ext = os.path.splitext(source_path)[1].lower()
    # ".ecad+" is not a real os.path split (splitext keeps only ".ecad+" when it is the suffix),
    # so also match the compound suffix explicitly.
    return ext in _BOARD_EXTS or source_path.lower().endswith(".ecad+")


def _iter_components(asm: Any):
    """Yield component dicts from an aicad.asm/1 document tree, tolerant of nesting."""
    if not isinstance(asm, dict):
        return
    node = asm
    if isinstance(asm.get("tree"), dict):
        node = asm["tree"]
    comps = node.get("components")
    if isinstance(comps, list):
        for c in comps:
            if isinstance(c, dict):
                yield c


# --- Default (core-backed) loaders — not exercised in CI (no DLL / no board file). ------------

def _default_load_assembly(doc_path: str) -> dict:
    """Load the assembly document tree via the C++ core (``doc_tree``)."""
    from mcp_server import core_bridge as cb  # lazy: keep the DLL bridge off the import path
    res = cb.call("doc_open", {"path": str(doc_path)})
    doc_id = res.get("doc_id") if isinstance(res, dict) else None
    tree = cb.call("doc_tree", {"doc_id": doc_id})
    return tree if isinstance(tree, dict) else {}


def _default_load_board(board_path: str) -> Optional[dict]:
    """Load an ECAD board document dict from disk (``engine.ecad.formats.open``)."""
    from engine.ecad import formats  # lazy
    doc, _warnings = formats.open(board_path)
    return doc if isinstance(doc, dict) else None


def _footprint_mpn_index(board: dict) -> dict[str, str]:
    """Map each footprint ``ref`` -> its manufacturer part number (from ``fields.MPN`` or ``mpn``)."""
    idx: dict[str, str] = {}
    for fp in board.get("footprints", []) or []:
        if not isinstance(fp, dict):
            continue
        ref = fp.get("ref")
        mpn = None
        fields = fp.get("fields")
        if isinstance(fields, dict):
            mpn = fields.get("MPN") or fields.get("mpn")
        mpn = mpn or fp.get("mpn")
        if ref and mpn:
            idx[str(ref)] = str(mpn)
    return idx


def _ecad_bom_rows(board: dict) -> list[dict]:
    """Grouped electronic-BOM rows (Refs/Qty/Value/Footprint/MPN) for a board.

    Grouping, quantities and DNP exclusion come from the real ``write_bom`` CSV (DoD: "BOM CSV
    merged"). The CSV's MPN column is enriched from the board footprints keyed by ref: the
    ``write_bom`` grouper drops the MPN before emitting it, so recovering it from the source
    board — without mutating that shared exporter — keeps electronic lines priceable.
    """
    from engine.ecad.fab import bom as ecad_bom  # lazy
    import csv as _csv
    import io as _io
    text = ecad_bom.write_bom(board, fmt="csv")   # CSV per the DoD ("BOM CSV merged")
    rows = list(_csv.DictReader(_io.StringIO(text)))
    ref_to_mpn = _footprint_mpn_index(board)
    for row in rows:
        if not (row.get("MPN") or "").strip():
            first_ref = (row.get("Refs") or "").split(",")[0].strip()
            mpn = ref_to_mpn.get(first_ref)
            if mpn:
                row["MPN"] = mpn
    return rows


# --- Roll-up ----------------------------------------------------------------------------------

class _Ids:
    def __init__(self) -> None:
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return f"L{self._n}"


def rollup(
    doc_path: str,
    *,
    load_assembly: Optional[Callable[[str], dict]] = None,
    load_board: Optional[Callable[[str], Optional[dict]]] = None,
    vendor_lookup: Optional[Callable[[dict], Optional[dict]]] = None,
) -> list[BomLine]:
    """Walk the assembly at ``doc_path`` (+ any linked ECAD board) into a unified, unpriced BOM.

    Each mechanical component is classified **bought** when it carries a manufacturer part number
    / supplier or matches a registered ``VendorPart`` (``vendor_lookup``), else **made**. A
    component whose source is a board doc (``.ecad``/``.ecad+``/``.kicad_pcb``) is expanded into
    one bought line per grouped footprint via the ECAD BOM CSV. Repeated instances of the same
    part are aggregated (their quantities summed). Pricing is *not* resolved here — call
    :func:`resolve_prices` for that.

    ``load_assembly`` / ``load_board`` / ``vendor_lookup`` are injectable seams; the defaults use
    the C++ core, the on-disk board loader, and the vendor-part registry respectively. Tests
    inject in-memory fixtures so the roll-up runs with no DLL and no network.
    """
    load_assembly = load_assembly or _default_load_assembly
    load_board = load_board or _default_load_board

    asm = load_assembly(doc_path)
    ids = _Ids()
    # Ordered map so repeated instances aggregate while encounter order is preserved.
    groups: dict[str, BomLine] = {}
    board_paths: list[str] = []

    for comp in _iter_components(asm):
        source_path = _component_source_path(comp)
        if _is_board(source_path):
            board_paths.append(source_path)
            continue

        qty = _as_qty(comp.get("quantity"))
        mpn = _component_mpn(comp)
        supplier = _component_supplier(comp)
        rec = vendor_lookup(comp) if vendor_lookup is not None else None
        is_bought = bool(mpn or supplier or rec)

        if is_bought:
            part_number = mpn or (rec or {}).get("part_number")
            sup = supplier or (rec or {}).get("supplier")
            part_ref = _vendor_key(sup, part_number) if (sup and part_number) else None
            gkey = f"bought:{part_ref or mpn or source_path or _component_description(comp)}"
            existing = groups.get(gkey)
            if existing is not None:
                existing.quantity += qty
            else:
                groups[gkey] = BomLine(
                    line_id=ids.next(), kind="bought", quantity=qty, unit="ea",
                    part_ref=part_ref, vendor_part=rec, mpn=mpn or (str(part_number) if part_number else None),
                    description=_component_description(comp),
                )
        else:
            gkey = f"made:{source_path or _component_description(comp)}"
            existing = groups.get(gkey)
            if existing is not None:
                existing.quantity += qty
            else:
                groups[gkey] = BomLine(
                    line_id=ids.next(), kind="made", quantity=qty, unit="ea",
                    description=_component_description(comp),
                )

    lines = list(groups.values())

    # Merge each linked board's electronic BOM (bought lines; one per grouped footprint).
    for bp in board_paths:
        board = load_board(bp)
        if not board:
            continue
        for row in _ecad_bom_rows(board):
            lines.append(_ecad_row_to_line(row, ids))

    return lines


def _ecad_row_to_line(row: dict, ids: _Ids) -> BomLine:
    mpn = (row.get("MPN") or "").strip() or None
    value = (row.get("Value") or "").strip()
    footprint = (row.get("Footprint") or "").strip()
    refs = (row.get("Refs") or "").strip()
    desc = " ".join(p for p in (value, footprint) if p) or refs or "component"
    return BomLine(
        line_id=ids.next(), kind="bought", quantity=_as_qty(row.get("Qty")), unit="ea",
        part_ref=None, vendor_part=None, mpn=mpn, description=desc,
    )


# --- Pricing ----------------------------------------------------------------------------------

def resolve_prices(
    bom: list[BomLine],
    *,
    chain: Any = None,
) -> list[BomLine]:
    """Resolve prices for the bought lines of ``bom`` in place, returning the same list.

    For each bought line with a manufacturer part number, call ``chain.fetch(mpn)`` (default: the
    Nexar→Digi-Key→Mouser :class:`~engine.parts_providers.ProviderChain`). On a hit the line's
    ``vendor_part``/``part_ref`` are populated and ``unit_cost``/``ext_cost`` resolved from the
    record's ``price_breaks`` for ``quantity`` (contract §1/§3). When the chain declines (returns
    ``None``) the line's price stays *unknown* (``unit_cost``/``ext_cost`` remain ``None``) — never
    a fabricated number. Made lines are untouched (``cost_model`` filled by T-B3).
    """
    if chain is None:
        chain = _default_chain()
    for line in bom:
        if line.kind != "bought" or not line.mpn:
            continue
        rec = chain.fetch(line.mpn)
        if not rec:
            continue  # price unknown — contract §0: None is "not known", never guessed
        line.vendor_part = rec
        key = _vendor_key(rec.get("supplier"), rec.get("part_number"))
        if key:
            line.part_ref = key
        unit = _unit_price_for_qty(rec, line.quantity)
        if unit is not None:
            line.unit_cost = float(unit)
            line.ext_cost = float(unit) * int(line.quantity)
            cur = rec.get("currency")
            if cur:
                line.currency = str(cur)
    return bom


def _unit_price_for_qty(rec: dict, quantity: int) -> Optional[float]:
    """Highest ``price_breaks`` tier whose ``qty <= quantity``, falling back to ``unit_price``."""
    breaks = sorted(
        (b for b in (rec.get("price_breaks") or []) if isinstance(b, dict)),
        key=lambda b: b.get("qty", 0),
    )
    best: Optional[float] = None
    for b in breaks:
        if b.get("qty", 0) <= quantity and b.get("unit_price") is not None:
            best = float(b["unit_price"])
    if best is not None:
        return best
    up = rec.get("unit_price")
    return float(up) if up is not None else None


def _vendor_key(supplier: Any, part_number: Any) -> Optional[str]:
    if not supplier or not part_number:
        return None
    from engine.vendor_parts import slug  # lazy: registry helper, no heavy import
    return f"{slug(str(supplier))}::{slug(str(part_number))}"


def _default_chain():
    from engine.parts_providers import default_pricing_chain  # lazy: keep HTTP surface off import
    return default_pricing_chain()


def _as_qty(value: Any) -> int:
    try:
        q = int(value)
    except (TypeError, ValueError):
        return 1
    return q if q >= 1 else 1


# --- Serialization ----------------------------------------------------------------------------

def to_dicts(bom: list[BomLine]) -> list[dict]:
    """Marshal BOM lines to JSON-serializable dicts (``dataclasses.asdict``)."""
    return [asdict(line) for line in bom]
