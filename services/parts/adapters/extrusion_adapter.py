"""CatalogProvider adapter for T-slot aluminum extrusion profiles.

Covers the standard 80/20-style T-slot framing profile sizes: metric
20x20 / 20x40 / 30x30 / 30x60 / 40x40 / 40x80, and imperial fractional
1010 (1"x1") / 1515 (1.5"x1.5"). "80/20" is a genericized trademark for this
entire modular T-slot extrusion product category (see 80/20 Inc.'s own
naming-convention guide) — every major supplier (80/20 Inc., Parker IPS,
item, Bosch Rexroth, misumi, ...) ships dimensionally-compatible profiles at
these same nominal sizes, slot widths, and hole patterns.

Domain: "section" (structural-section sweep — see contracts/parts.md §2
`section_profile`), matching services/parts/taxonomy.json's
"structural_stock.extrusions_tslot" node.

Moment of inertia (Ix/Iy) is a CALCULATED nominal — a thin-wall hollow-
rectangle approximation of each profile's outer envelope, not a transcription
of any single vendor's exact (proprietary) cross-section datasheet. Multiple
distributor listings for these part numbers disagree with each other by
1-2 orders of magnitude (some exceed the solid-envelope physical upper bound,
i.e. are simply wrong), so a computed nominal — clearly labeled as such in
`provenance` — is the honest choice here; always verify against the specific
manufacturer's CAD/datasheet before structural design, exactly as
heatsinks_adapter.py does for its thermal nominals.
"""
from __future__ import annotations

from typing import Iterator

_DOMAIN = "section"
_MM_PER_IN = 25.4


def _wall_mm(w_mm: float, h_mm: float) -> float:
    """Approximate T-slot wall thickness: ~9% of the smaller envelope side,
    clamped to a sane minimum — matches published wall thickness for these
    profile families within roughly 2x (thin-wall hollow-tube approximation,
    not exact vendor geometry)."""
    return max(1.5, round(min(w_mm, h_mm) * 0.09, 2))


def _hollow_rect_inertia(w_mm: float, h_mm: float, t_mm: float) -> tuple[float, float]:
    """(Ix, Iy) in mm^4 for a thin-wall hollow rectangle W x H, wall t."""
    w2, h2 = w_mm - 2 * t_mm, h_mm - 2 * t_mm
    if w2 <= 0 or h2 <= 0:
        w2 = h2 = 0.0
    ix = (w_mm * h_mm ** 3 - w2 * h2 ** 3) / 12.0
    iy = (h_mm * w_mm ** 3 - h2 * w2 ** 3) / 12.0
    return ix, iy


# (series, w_mm, h_mm, slot_mm, is_metric, synonyms)
_PROFILES: tuple = (
    ("2020", 20, 20, 6, True,
     "2020 extrusion t-slot tslot t slot 8020 80/20 20x20 20 series aluminum framing"),
    ("2040", 20, 40, 6, True,
     "2040 extrusion t-slot tslot t slot 8020 80/20 20x40 20 series aluminum framing"),
    ("3030", 30, 30, 8, True,
     "3030 extrusion t-slot tslot t slot 8020 80/20 30x30 30 series aluminum framing"),
    ("3060", 30, 60, 8, True,
     "3060 extrusion t-slot tslot t slot 8020 80/20 30x60 30 series aluminum framing"),
    ("4040", 40, 40, 8, True,
     "4040 extrusion t-slot tslot t slot 8020 80/20 40x40 40 series aluminum framing"),
    ("4080", 40, 80, 8, True,
     "4080 extrusion t-slot tslot t slot 8020 80/20 40x80 40 series aluminum framing"),
    ("1010", 25.4, 25.4, 8.9, False,
     "1010 extrusion t-slot tslot t slot 8020 80/20 1x1 10 series aluminum framing "
     "imperial fractional"),
    ("1515", 38.1, 38.1, 8.9, False,
     "1515 extrusion t-slot tslot t slot 8020 80/20 1.5x1.5 15 series aluminum framing "
     "imperial fractional"),
)

_ALLOY = "6105-T5"
_ALLOY_SYN = "aluminum aluminium 6105 6105-t5 t-slot extrusion aluminum profile"


def _make_record(series: str, w_mm: float, h_mm: float, slot_mm: float,
                  is_metric: bool, synonyms: str) -> dict:
    t = _wall_mm(w_mm, h_mm)
    ix, iy = _hollow_rect_inertia(w_mm, h_mm, t)
    w_in, h_in = w_mm / _MM_PER_IN, h_mm / _MM_PER_IN
    size_label = (f"{w_mm:g}mm x {h_mm:g}mm" if is_metric
                  else f"{w_in:g}\" x {h_in:g}\"")
    title = f"{series} T-Slot Aluminum Extrusion {size_label} {synonyms} {_ALLOY_SYN}"
    params = {
        "series": series,
        "width_mm": w_mm,
        "height_mm": h_mm,
        "width_in": round(w_in, 4),
        "height_in": round(h_in, 4),
        "slot_width_mm": slot_mm,
        "slot_width_in": round(slot_mm / _MM_PER_IN, 4),
        "wall_thickness_mm": t,
        "moment_of_inertia_ix_mm4": round(ix, 1),
        "moment_of_inertia_iy_mm4": round(iy, 1),
        "moment_of_inertia_ix_in4": round(ix / _MM_PER_IN ** 4, 4),
        "moment_of_inertia_iy_in4": round(iy / _MM_PER_IN ** 4, 4),
        "material": "6105-T5",
        "material_label": "Aluminum 6105-T5",
        "standard": "T-SLOT",
        "nominal_size": series,
    }
    return {
        "part_id": f"section:T-SLOT:{series}",
        "domain": _DOMAIN,
        "title": title,
        "standard": "T-SLOT",
        "designation": series,
        "params": params,
        "geometry_ref": {
            "kind": "section_profile",
            "standard": "T-SLOT",
            "designation": series,
        },
        "sourcing": [],
        "provenance": (
            "Nominal envelope size and slot width from the widely-used, "
            "genericized 80/20-style T-slot framing profile family (compatible "
            "across 80/20 Inc., Parker IPS, item, Bosch Rexroth, misumi, and "
            "others at this nominal size). Moment of inertia is a CALCULATED "
            "thin-wall hollow-rectangle nominal (not a transcribed vendor "
            "datasheet value — published third-party listings for these part "
            "numbers were found to disagree by orders of magnitude and some "
            "exceed the solid-envelope physical bound); verify against the "
            "specific manufacturer's datasheet before structural design. See "
            "contracts/licenses.md."
        ),
        "license": "public-domain",
    }


class ExtrusionAdapter:
    """CatalogProvider for T-slot aluminum extrusion profiles (domain "section")."""

    domain: str = _DOMAIN
    provider_id: str = "aluminum_extrusion"

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
        if not part_id or not part_id.startswith("section:T-SLOT:"):
            return None
        series = part_id[len("section:T-SLOT:"):]
        for spec in _PROFILES:
            if spec[0] == series:
                return _make_record(*spec)
        return None

    def count(self) -> int:
        return len(_PROFILES)

    def iter_records(self) -> Iterator[dict]:
        for spec in _PROFILES:
            yield _make_record(*spec)
