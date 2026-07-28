"""CatalogProvider adapter for timing belts and pulleys (motion/power-transmission
coverage gap: corpus token counts for "belt"/"pulley" were 0 before this adapter).

Generated programmatically from the published pitch geometry of the common
synchronous-belt profile families:
  - GT2 / GT3   ("2GT"/"3GT"): modified-curvilinear tooth profile, 2 mm / 3 mm pitch.
  - HTD 3M / 5M: circular-arc HTD tooth profile, 3 mm / 5 mm pitch.
  - T5:          metric trapezoidal tooth profile, 5 mm pitch.
  - MXL:         imperial trapezoidal tooth profile, 0.08 in (2.032 mm) pitch.
Domain: "motion".

PROVENANCE / HONESTY: pitch values are the published profile-standard numbers (fixed
by the tooth-form spec, not a vendor choice). Tooth counts, bore sizes and belt
widths are representative commercially-common values (the sizes a distributor
catalog would stock), not an exhaustive vendor SKU list. Closed-loop belt length is
computed exactly as teeth_count * pitch_mm (definitionally exact, not interpolated).
"""
from __future__ import annotations

from typing import Iterator

_DOMAIN = "motion"

# profile_id -> (label, pitch_mm, tooth_form, pulley_bores_mm, belt_widths_mm, pulley_teeth)
_PROFILES: dict[str, tuple] = {
    "GT2": (
        "GT2 2mm pitch", 2.0, "modified curvilinear",
        [4.0, 5.0, 6.35], [6.0, 9.0, 10.0],
        [10, 12, 16, 20, 24, 32, 40, 60],
    ),
    "GT3": (
        "GT3 3mm pitch", 3.0, "modified curvilinear",
        [5.0, 6.0, 8.0], [6.0, 9.0, 15.0],
        [12, 15, 20, 24, 30, 40, 60],
    ),
    "HTD3M": (
        "HTD 3M 3mm pitch", 3.0, "circular arc HTD",
        [6.0, 8.0], [6.0, 9.0, 15.0],
        [12, 15, 20, 24, 30, 45, 60],
    ),
    "HTD5M": (
        "HTD 5M 5mm pitch", 5.0, "circular arc HTD",
        [8.0, 10.0, 12.0], [9.0, 15.0, 25.0],
        [15, 18, 24, 30, 36, 45, 60],
    ),
    "T5": (
        "T5 5mm pitch trapezoidal", 5.0, "trapezoidal",
        [6.0, 8.0, 10.0], [6.0, 10.0, 16.0, 25.0],
        [10, 12, 15, 20, 24, 30, 40],
    ),
    "MXL": (
        "MXL 0.08in pitch trapezoidal", 2.032, "trapezoidal (imperial)",
        [4.0, 5.0, 6.35], [3.2, 4.8],
        [10, 12, 16, 20, 24, 30, 40],
    ),
}

# Common commercially-stocked closed-loop belt teeth counts, shared across profiles
# (actual belt length = teeth_count * profile pitch_mm — see _make_belt_record).
_BELT_LOOP_TEETH = [80, 100, 120, 140, 160, 200, 250]

_KEYWORDS = "timing belt toothed belt synchronous belt pulley timing pulley idler pulley gt2 2gt"


def _make_pulley_record(profile_id: str, label: str, pitch_mm: float, tooth_form: str,
                         teeth: int, bore_mm: float, belt_width_mm: float,
                         compatible_widths_mm: list) -> dict:
    pitch_dia_mm = round((pitch_mm * teeth) / 3.14159265358979, 3)
    designation = f"{profile_id}-{teeth}T-{bore_mm:g}mm"
    part_id = f"{_DOMAIN}:pulley:{designation}"
    widths_txt = "/".join(f"{w:g}" for w in compatible_widths_mm)
    title = (
        f"{label} timing pulley {teeth} tooth, {bore_mm:g}mm bore, "
        f"{widths_txt}mm belt width"
    )
    text = f"{profile_id.lower()} {_KEYWORDS} {tooth_form} synchronous drive pulley"

    params = {
        "profile": profile_id,
        "pitch_mm": pitch_mm,
        "tooth_form": tooth_form,
        "teeth": teeth,
        "bore_mm": bore_mm,
        "belt_width_mm": belt_width_mm,
        "compatible_widths_mm": list(compatible_widths_mm),
        "pitch_dia_mm": pitch_dia_mm,
        "part_type": "pulley",
    }

    return {
        "part_id":      part_id,
        "domain":       _DOMAIN,
        "title":        f"{title} {text}",
        "standard":     profile_id,
        "designation":  designation,
        "params":       params,
        "geometry_ref": {
            "kind": "parametric_timing_pulley",
            "spec": {
                "profile": profile_id, "pitch_mm": pitch_mm, "teeth": teeth,
                "bore_mm": bore_mm, "belt_width_mm": belt_width_mm,
            },
        },
        "sourcing":     [],
        "provenance":   (
            f"{profile_id} is a published synchronous-belt tooth-profile standard; "
            f"pitch ({pitch_mm} mm) is fixed by the profile spec. Tooth count, bore "
            "and belt width are representative commercially-common stock sizes, "
            "not an exhaustive vendor SKU list. Pitch diameter is computed exactly "
            "(teeth * pitch / pi)."
        ),
        "license":      "curated-nominal",
    }


def _make_belt_record(profile_id: str, label: str, pitch_mm: float, tooth_form: str,
                       teeth: int, belt_width_mm: float) -> dict:
    length_mm = round(teeth * pitch_mm, 3)
    designation = f"{profile_id}-{length_mm:g}mm-{belt_width_mm:g}mm"
    part_id = f"{_DOMAIN}:belt:{designation}"
    title = (
        f"{label} closed-loop timing belt, {length_mm:g}mm length "
        f"({teeth} teeth), {belt_width_mm:g}mm width"
    )
    text = f"{profile_id.lower()} {_KEYWORDS} {tooth_form} endless loop belt"

    params = {
        "profile": profile_id,
        "pitch_mm": pitch_mm,
        "tooth_form": tooth_form,
        "teeth": teeth,
        "length_mm": length_mm,
        "belt_width_mm": belt_width_mm,
        "part_type": "belt",
    }

    return {
        "part_id":      part_id,
        "domain":       _DOMAIN,
        "title":        f"{title} {text}",
        "standard":     profile_id,
        "designation":  designation,
        "params":       params,
        "geometry_ref": {
            "kind": "parametric_timing_belt",
            "spec": {
                "profile": profile_id, "pitch_mm": pitch_mm, "teeth": teeth,
                "length_mm": length_mm, "belt_width_mm": belt_width_mm,
            },
        },
        "sourcing":     [],
        "provenance":   (
            f"{profile_id} is a published synchronous-belt tooth-profile standard; "
            f"pitch ({pitch_mm} mm) is fixed by the profile spec. Closed-loop length "
            "is computed exactly as teeth_count * pitch_mm (definitionally exact). "
            "Teeth count and belt width are representative commercially-common "
            "stock sizes, not an exhaustive vendor SKU list."
        ),
        "license":      "curated-nominal",
    }


class BeltsPulleysAdapter:
    """CatalogProvider for GT2/GT3/HTD/T5/MXL timing belts and pulleys (domain "motion")."""

    domain: str = _DOMAIN
    provider_id: str = "belts_pulleys"

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
        for profile_id, (label, pitch_mm, tooth_form, bores, widths, teeth_list) in _PROFILES.items():
            for teeth in teeth_list:
                for bore_mm in bores:
                    yield _make_pulley_record(
                        profile_id, label, pitch_mm, tooth_form,
                        teeth, bore_mm, widths[0], widths,
                    )
            for loop_teeth in _BELT_LOOP_TEETH:
                for belt_width_mm in widths:
                    yield _make_belt_record(
                        profile_id, label, pitch_mm, tooth_form,
                        loop_teeth, belt_width_mm,
                    )
