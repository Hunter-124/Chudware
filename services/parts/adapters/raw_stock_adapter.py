"""CatalogProvider adapter for raw metal/plastic stock.

Generates round bar, hex bar, flat bar, plate/sheet, and round tube records
across common engineering alloys and plastics, spanning the standard imperial
(fractional-inch) and metric size ladders. Domain: "section" — the existing
reserved domain (contracts/parts.md §0), already routed here by
services/parts/taxonomy.json's "structural_stock" category (which explicitly
covers "Raw stock: metal tube/bar/sheet ... and plastic stock").

Alloy/material designations (6061-T6, 7075-T6, 1018, 4140, 304, 316, 360
brass, acetal/Delrin, HDPE, PTFE, acrylic, polycarbonate) are standard,
publicly documented engineering material names — not invented. Cross-section
dimensions are drawn from common commercial stock-size ladders (not a single
vendor's SKU list); this is a nominal parametric catalog, not a scrape of any
distributor's catalog.

No rewrite of any existing surface — new, self-contained local catalog,
following the same generation pattern as structural_sections_adapter.py.
"""
from __future__ import annotations

from typing import Iterator

_DOMAIN = "section"

_MM_PER_IN = 25.4

# (code, label, density_g_cm3, synonyms, std_designation)
_METALS: tuple = (
    ("6061-T6", "Aluminum 6061-T6", 2.70,
     "aluminum aluminium al 6061 6061-t6 6061t6 t6", "AA/ASTM B221"),
    ("7075-T6", "Aluminum 7075-T6", 2.81,
     "aluminum aluminium al 7075 7075-t6 7075t6 aircraft aluminum", "AA/ASTM B211"),
    ("1018", "Steel 1018", 7.87,
     "steel mild steel low carbon steel crs cold rolled steel 1018", "ASTM A108"),
    ("4140", "Steel 4140", 7.85,
     "steel chromoly chrome moly alloy steel prehard 4140", "ASTM A29/A322"),
    ("304", "Stainless Steel 304", 8.00,
     "stainless steel ss304 304 18-8 304 stainless", "ASTM A276"),
    ("316", "Stainless Steel 316", 8.00,
     "stainless steel ss316 316 marine grade stainless 316l", "ASTM A276"),
    ("360-brass", "Brass 360", 8.50,
     "brass free machining brass c360 360 brass bar", "ASTM B16"),
)

_PLASTICS: tuple = (
    ("acetal", "Acetal (Delrin)", 1.41,
     "acetal delrin pom polyoxymethylene", "ASTM D6100"),
    ("hdpe", "HDPE", 0.95,
     "hdpe high density polyethylene poly plastic", "ASTM D4976"),
    ("ptfe", "PTFE", 2.20,
     "ptfe teflon fluoropolymer", "ASTM D1710"),
    ("acrylic", "Acrylic (PMMA)", 1.19,
     "acrylic plexiglass pmma lucite plastic sheet", "ASTM D4802"),
    ("polycarbonate", "Polycarbonate (PC)", 1.20,
     "polycarbonate lexan pc plastic sheet", "ASTM D3935"),
)

# ---------------------------------------------------------------------------
# Size ladders (native unit feeds the generator; the other unit is always
# computed and carried in params).
# ---------------------------------------------------------------------------

_ROUND_DIA_IN = (0.125, 0.1875, 0.25, 0.3125, 0.375, 0.4375, 0.5, 0.625,
                  0.75, 0.875, 1.0, 1.25, 1.5, 2.0)
_ROUND_DIA_MM = (5, 6, 8, 10, 12, 16, 20, 25, 30, 40, 50)

_HEX_AF_IN = (0.25, 0.3125, 0.375, 0.5, 0.625, 0.75, 1.0)
_HEX_AF_MM = (6, 8, 10, 12, 17, 19, 22, 24, 30)

# (thickness_in, width_in)
_FLAT_BAR_IN = ((0.125, 0.5), (0.1875, 0.75), (0.25, 0.5), (0.25, 0.75),
                (0.25, 1.0), (0.375, 1.0), (0.375, 1.5), (0.5, 1.0),
                (0.5, 2.0), (0.75, 2.0), (1.0, 2.0))
# (thickness_mm, width_mm)
_FLAT_BAR_MM = ((3, 12), (5, 20), (6, 25), (6, 40), (8, 25), (10, 40),
                (10, 50), (12, 50), (20, 50))

_PLATE_THICK_IN = (0.0625, 0.125, 0.1875, 0.25, 0.375, 0.5, 0.75, 1.0)
_PLATE_THICK_MM = (1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25)

# (od_in, wall_in)
_TUBE_IN = ((0.25, 0.035), (0.375, 0.049), (0.5, 0.065), (0.625, 0.065),
            (0.75, 0.065), (1.0, 0.083), (1.25, 0.083), (1.5, 0.095),
            (2.0, 0.120))
# (od_mm, wall_mm)
_TUBE_MM = ((6, 1), (8, 1), (10, 1), (12, 1.5), (16, 1.5), (20, 2),
            (25, 2), (32, 2), (40, 3), (50, 3))


def _fmt_in(v: float) -> str:
    return f"{v:g}in"


def _fmt_mm(v: float) -> str:
    return f"{v:g}mm"


def _stock_record(mat: tuple, shape_std: str, size_key: str, size_title: str,
                   dims: dict, synonyms: str) -> dict:
    code, label, density, mat_syn, standard = mat
    designation = f"{code}:{shape_std}:{size_key}"
    part_id = f"section:{shape_std}:{code}:{size_key}"
    title = f"{size_title} {mat_syn} {synonyms}"
    params: dict = dict(dims)
    params.update({
        "material": code,
        "material_label": label,
        "density_g_cm3": density,
        "standard": standard,
        "nominal_size": size_title,
    })
    return {
        "part_id": part_id,
        "domain": _DOMAIN,
        "title": title,
        "standard": standard,
        "designation": designation,
        "params": params,
        "geometry_ref": {
            "kind": "section_profile",
            "standard": shape_std,
            "designation": designation,
        },
        "sourcing": [],
        "provenance": (
            "Nominal cross-section dimensions from common commercial stock-size "
            "ladders (imperial fractional-inch and metric); material designation "
            "is a standard, publicly documented engineering alloy/plastic name "
            "(AA/ASTM/ASME wrought-alloy and plastics nomenclature), not a "
            "vendor SKU list. See contracts/licenses.md."
        ),
        "license": "public-domain",
    }


def _round_bar_records(mat: tuple) -> Iterator[dict]:
    for dia in _ROUND_DIA_IN:
        yield _stock_record(
            mat, "ROUND-BAR", _fmt_in(dia),
            f"{dia:g}\" dia round bar / rod {mat[1]}",
            {"shape": "round_bar", "diameter_in": dia,
             "diameter_mm": round(dia * _MM_PER_IN, 3)},
            "round bar rod barstock stock round stock")
    for dia in _ROUND_DIA_MM:
        yield _stock_record(
            mat, "ROUND-BAR", _fmt_mm(dia),
            f"{dia:g}mm dia round bar / rod {mat[1]}",
            {"shape": "round_bar", "diameter_mm": dia,
             "diameter_in": round(dia / _MM_PER_IN, 4)},
            "round bar rod barstock stock round stock")


def _hex_bar_records(mat: tuple) -> Iterator[dict]:
    for af in _HEX_AF_IN:
        yield _stock_record(
            mat, "HEX-BAR", _fmt_in(af),
            f"{af:g}\" across flats hex bar {mat[1]}",
            {"shape": "hex_bar", "across_flats_in": af,
             "across_flats_mm": round(af * _MM_PER_IN, 3)},
            "hex bar hexagon bar hex stock hexagonal barstock")
    for af in _HEX_AF_MM:
        yield _stock_record(
            mat, "HEX-BAR", _fmt_mm(af),
            f"{af:g}mm across flats hex bar {mat[1]}",
            {"shape": "hex_bar", "across_flats_mm": af,
             "across_flats_in": round(af / _MM_PER_IN, 4)},
            "hex bar hexagon bar hex stock hexagonal barstock")


def _flat_bar_records(mat: tuple) -> Iterator[dict]:
    for t, w in _FLAT_BAR_IN:
        yield _stock_record(
            mat, "FLAT-BAR", f"{_fmt_in(t)}x{_fmt_in(w)}",
            f"{t:g}\" x {w:g}\" flat bar {mat[1]}",
            {"shape": "flat_bar", "thickness_in": t, "width_in": w,
             "thickness_mm": round(t * _MM_PER_IN, 3),
             "width_mm": round(w * _MM_PER_IN, 3)},
            "flat bar rectangular bar bar stock barstock")
    for t, w in _FLAT_BAR_MM:
        yield _stock_record(
            mat, "FLAT-BAR", f"{_fmt_mm(t)}x{_fmt_mm(w)}",
            f"{t:g}mm x {w:g}mm flat bar {mat[1]}",
            {"shape": "flat_bar", "thickness_mm": t, "width_mm": w,
             "thickness_in": round(t / _MM_PER_IN, 4),
             "width_in": round(w / _MM_PER_IN, 4)},
            "flat bar rectangular bar bar stock barstock")


def _plate_sheet_records(mat: tuple) -> Iterator[dict]:
    for t in _PLATE_THICK_IN:
        kind = "plate" if t >= 0.25 else "sheet"
        yield _stock_record(
            mat, "PLATE-SHEET", _fmt_in(t),
            f"{t:g}\" thick {kind} {mat[1]}",
            {"shape": kind, "thickness_in": t,
             "thickness_mm": round(t * _MM_PER_IN, 3)},
            "plate sheet flat stock sheet stock plate stock")
    for t in _PLATE_THICK_MM:
        kind = "plate" if t >= 6 else "sheet"
        yield _stock_record(
            mat, "PLATE-SHEET", _fmt_mm(t),
            f"{t:g}mm thick {kind} {mat[1]}",
            {"shape": kind, "thickness_mm": t,
             "thickness_in": round(t / _MM_PER_IN, 4)},
            "plate sheet flat stock sheet stock plate stock")


def _round_tube_records(mat: tuple) -> Iterator[dict]:
    for od, wall in _TUBE_IN:
        yield _stock_record(
            mat, "ROUND-TUBE", f"{_fmt_in(od)}x{wall:g}in-wall",
            f"{od:g}\" OD x {wall:g}\" wall round tube {mat[1]}",
            {"shape": "round_tube", "od_in": od, "wall_in": wall,
             "id_in": round(od - 2 * wall, 4),
             "od_mm": round(od * _MM_PER_IN, 3),
             "wall_mm": round(wall * _MM_PER_IN, 3)},
            "round tube tubing pipe stock")
    for od, wall in _TUBE_MM:
        yield _stock_record(
            mat, "ROUND-TUBE", f"{_fmt_mm(od)}x{wall:g}mm-wall",
            f"{od:g}mm OD x {wall:g}mm wall round tube {mat[1]}",
            {"shape": "round_tube", "od_mm": od, "wall_mm": wall,
             "id_mm": round(od - 2 * wall, 3),
             "od_in": round(od / _MM_PER_IN, 4),
             "wall_in": round(wall / _MM_PER_IN, 4)},
            "round tube tubing pipe stock")


_SHAPE_GENERATORS = (
    _round_bar_records, _hex_bar_records, _flat_bar_records,
    _plate_sheet_records, _round_tube_records,
)


class RawStockAdapter:
    """CatalogProvider for raw metal/plastic stock (domain "section")."""

    domain: str = _DOMAIN
    provider_id: str = "raw_stock"

    def search(self, query: str, *,
               domain: str | None = None,
               filters: dict | None = None) -> list[dict]:
        if domain and domain != _DOMAIN:
            return []
        norm = (query or "").strip().lower()
        recs = list(self.iter_records())
        if not norm:
            return recs
        return [r for r in recs if norm in r["title"].lower()]

    def get(self, part_id: str) -> dict | None:
        for rec in self.iter_records():
            if rec["part_id"] == part_id:
                return rec
        return None

    def count(self) -> int:
        return sum(1 for _ in self.iter_records())

    def iter_records(self) -> Iterator[dict]:
        for mat in _METALS:
            for gen in _SHAPE_GENERATORS:
                yield from gen(mat)
        for mat in _PLASTICS:
            yield from _round_bar_records(mat)
            yield from _plate_sheet_records(mat)
            yield from _round_tube_records(mat)
