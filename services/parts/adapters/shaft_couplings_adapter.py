"""CatalogProvider adapter for shaft couplers and shaft collars (motion/power-
transmission coverage gap: these families kept falling out of catalog search
even when the bore size and family name were both typed correctly).

Covers two related shaft-power basics, generated from a shared bore-size table
spanning the common metric and imperial shaft sizes:
  - Shaft couplers: rigid set-screw couplers and split-clamp ("clamping") rigid
    couplers, same-bore and common step-bore pairs.
  - Shaft collars: one-piece set-screw collars and two-piece clamp-style collars.
Domain: "motion".

PROVENANCE / HONESTY: bore sizes are the standard metric/imperial shaft diameters
stocked industry-wide. Body outside-diameter and length/width are representative
engineering-nominal dimensions scaled from the bore (in the style of common
coupler/collar catalogs — Ruland, Climax, etc.), NOT a specific vendor's exact
published dimension. Verify against the target part's datasheet before release.
"""
from __future__ import annotations

from typing import Iterator

_DOMAIN = "motion"

# Standard metric + imperial shaft bore sizes (mm). Imperial sizes are given in
# their exact metric equivalent (1/4in = 6.35mm, 3/8in = 9.525mm, 1/2in = 12.7mm,
# 3/4in = 19.05mm) so a single bore table serves both unit systems.
_BORES_MM: list[tuple[float, str]] = [
    (3.0, "3mm"), (4.0, "4mm"), (5.0, "5mm"), (6.0, "6mm"),
    (6.35, "1/4in"), (8.0, "8mm"), (9.525, "3/8in"), (10.0, "10mm"),
    (12.0, "12mm"), (12.7, "1/2in"), (14.0, "14mm"), (15.0, "15mm"),
    (16.0, "16mm"), (19.05, "3/4in"), (20.0, "20mm"), (25.0, "25mm"),
]

# Common step-bore (asymmetric) coupler pairs — the two most frequently paired
# bore sizes for e.g. "stepper motor shaft to leadscrew" reductions.
_COUPLER_STEP_PAIRS: list[tuple[float, float]] = [
    (5.0, 8.0), (5.0, 10.0), (6.35, 8.0), (6.35, 10.0), (8.0, 10.0), (8.0, 12.0),
]

_COUPLER_TYPES = [
    ("setscrew", "set-screw rigid shaft coupler", "rigid setscrew coupler shaft coupling"),
    ("clamp", "split-clamp rigid shaft coupler", "clamping shaft coupler clamp-style coupling"),
]

_COLLAR_TYPES = [
    ("setscrew", "one-piece set-screw shaft collar", "shaft collar set screw retaining collar"),
    ("clamp", "two-piece clamp-style shaft collar", "clamp collar split shaft collar retaining collar"),
]


def _coupler_dims(bore_a: float, bore_b: float) -> tuple[float, float]:
    """Representative (OD_mm, length_mm) for a coupler sized to the larger bore."""
    bmax = max(bore_a, bore_b)
    od_mm = round(bmax * 2.2 + 6.0, 1)
    length_mm = round(bmax * 3.0 + 10.0, 1)
    return od_mm, length_mm


def _collar_dims(bore_mm: float) -> tuple[float, float]:
    """Representative (OD_mm, width_mm) for a shaft collar at this bore."""
    od_mm = round(bore_mm * 1.8 + 6.0, 1)
    width_mm = round(max(3.0, bore_mm * 0.5 + 3.0), 1)
    return od_mm, width_mm


def _make_coupler_record(type_id: str, type_label: str, keywords: str,
                          bore_a: float, bore_a_label: str,
                          bore_b: float, bore_b_label: str) -> dict:
    od_mm, length_mm = _coupler_dims(bore_a, bore_b)
    designation = f"COUPLER-{type_id}-{bore_a_label}x{bore_b_label}".replace("/", "-")
    part_id = f"{_DOMAIN}:coupler:{designation}"
    same = bore_a == bore_b
    bore_txt = f"{bore_a_label} bore" if same else f"{bore_a_label} to {bore_b_label} step bore"
    title = f"{type_label}, {bore_txt}, {od_mm:g}mm OD x {length_mm:g}mm long"
    text = f"{keywords} coupling motor coupler leadscrew coupler"

    params = {
        "coupler_type": type_id,
        "bore_a_mm": bore_a,
        "bore_b_mm": bore_b,
        "od_mm": od_mm,
        "length_mm": length_mm,
        "part_type": "shaft_coupler",
    }

    return {
        "part_id":      part_id,
        "domain":       _DOMAIN,
        "title":        f"{title} {text}",
        "standard":     "coupler",
        "designation":  designation,
        "params":       params,
        "geometry_ref": {
            "kind": "parametric_shaft_coupler",
            "spec": {"coupler_type": type_id, "bore_a_mm": bore_a, "bore_b_mm": bore_b,
                      "od_mm": od_mm, "length_mm": length_mm},
        },
        "sourcing":     [],
        "provenance":   (
            "Bore sizes are standard stocked metric/imperial shaft diameters. "
            "OD and length are representative engineering-nominal dimensions "
            "scaled from the bore in the style of common coupler catalogs "
            "(Ruland/Climax-style set-screw and split-clamp rigid couplers), "
            "not a specific vendor's exact published dimension."
        ),
        "license":      "curated-nominal",
    }


def _make_collar_record(type_id: str, type_label: str, keywords: str,
                         bore_mm: float, bore_label: str) -> dict:
    od_mm, width_mm = _collar_dims(bore_mm)
    designation = f"COLLAR-{type_id}-{bore_label}".replace("/", "-")
    part_id = f"{_DOMAIN}:collar:{designation}"
    title = f"{type_label}, {bore_label} bore, {od_mm:g}mm OD x {width_mm:g}mm wide"
    text = f"{keywords} shaft stop locking collar"

    params = {
        "collar_type": type_id,
        "bore_mm": bore_mm,
        "od_mm": od_mm,
        "width_mm": width_mm,
        "part_type": "shaft_collar",
    }

    return {
        "part_id":      part_id,
        "domain":       _DOMAIN,
        "title":        f"{title} {text}",
        "standard":     "collar",
        "designation":  designation,
        "params":       params,
        "geometry_ref": {
            "kind": "parametric_shaft_collar",
            "spec": {"collar_type": type_id, "bore_mm": bore_mm, "od_mm": od_mm, "width_mm": width_mm},
        },
        "sourcing":     [],
        "provenance":   (
            "Bore sizes are standard stocked metric/imperial shaft diameters. "
            "OD and width are representative engineering-nominal dimensions "
            "scaled from the bore in the style of common shaft-collar catalogs "
            "(Ruland/Climax-style one-piece set-screw and two-piece clamp "
            "collars), not a specific vendor's exact published dimension."
        ),
        "license":      "curated-nominal",
    }


class ShaftCouplingsAdapter:
    """CatalogProvider for shaft couplers and shaft collars (domain "motion")."""

    domain: str = _DOMAIN
    provider_id: str = "shaft_couplings"

    def search(self, query: str, *, domain: str | None = None,
               filters: dict | None = None) -> list[dict]:
        if domain and domain != _DOMAIN:
            return []
        norm = (query or "").strip().lower()
        recs = list(self.iter_records())
        if not norm:
            return recs
        return [r for r in recs
                if norm in r["title"].lower() or norm in r["designation"].lower()]

    def get(self, part_id: str) -> dict | None:
        for rec in self.iter_records():
            if rec["part_id"] == part_id:
                return rec
        return None

    def count(self) -> int:
        return sum(1 for _ in self.iter_records())

    def iter_records(self) -> Iterator[dict]:
        for type_id, type_label, keywords in _COUPLER_TYPES:
            for bore_mm, bore_label in _BORES_MM:
                yield _make_coupler_record(type_id, type_label, keywords,
                                            bore_mm, bore_label, bore_mm, bore_label)
            for bore_a, bore_b in _COUPLER_STEP_PAIRS:
                label_a = next(lbl for m, lbl in _BORES_MM if m == bore_a)
                label_b = next(lbl for m, lbl in _BORES_MM if m == bore_b)
                yield _make_coupler_record(type_id, type_label, keywords,
                                            bore_a, label_a, bore_b, label_b)
        for type_id, type_label, keywords in _COLLAR_TYPES:
            for bore_mm, bore_label in _BORES_MM:
                yield _make_collar_record(type_id, type_label, keywords, bore_mm, bore_label)
