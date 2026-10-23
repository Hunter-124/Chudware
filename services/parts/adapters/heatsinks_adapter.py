"""CatalogProvider adapter for board-level heatsinks.

Small curated catalog of common plain-finned aluminum heatsinks (TO-220 clip-ons +
generic square SMD/push-pin extrusions) WITH thermal metadata, so the
``heatsink_select`` loop (services/mcp/heatsink.py) has library candidates to rank
against its parametric generator. Domain: "thermal".

Params per record (mm / g / C-per-W):
    l_mm, w_mm, h_mm        overall envelope
    base_t_mm, n_fins, fin_t_mm, fin_h_mm   plain-finned geometry (when applicable)
    theta_sa_c_per_w        sink-to-ambient resistance in NATURAL convection
                            (datasheet-class nominal at ~ the part's rated rise)
    theta_sa_forced_c_per_w forced-air (~2-3 m/s) nominal when published
    mass_g, cost_usd        mass + typical 1-off distributor price when known

PROVENANCE / HONESTY: numbers are curated engineering nominals in the style of the
named part families, entered for selection-loop ranking — ALWAYS verify against the
real datasheet before releasing a design (each record says so in ``provenance``).
Values without a trustworthy nominal are honest ``None`` (never invented).
"""
from __future__ import annotations

from typing import Iterator

_DOMAIN = "thermal"

# (designation, title, params, cost_usd)
_CATALOG: list = [
    ("HS-TO220-CLIP-19", "TO-220 clip-on heatsink 19x13x13 mm plain finned aluminum",
     {"l_mm": 19.0, "w_mm": 13.0, "h_mm": 12.7, "base_t_mm": 1.3, "n_fins": 6,
      "fin_t_mm": 1.0, "fin_h_mm": 9.0, "theta_sa_c_per_w": 24.0,
      "theta_sa_forced_c_per_w": 10.0, "mass_g": 4.0, "package": "TO-220"},
     0.60),
    ("HS-TO220-VERT-35", "TO-220 vertical-fin heatsink 35x13x33 mm aluminum",
     {"l_mm": 35.0, "w_mm": 12.7, "h_mm": 33.0, "base_t_mm": 2.0, "n_fins": 5,
      "fin_t_mm": 1.2, "fin_h_mm": 30.0, "theta_sa_c_per_w": 11.0,
      "theta_sa_forced_c_per_w": 4.5, "mass_g": 12.0, "package": "TO-220"},
     0.95),
    ("HS-BGA-14", "14x14x10 mm push-pin/adhesive BGA heatsink, plain finned",
     {"l_mm": 14.0, "w_mm": 14.0, "h_mm": 10.0, "base_t_mm": 2.0, "n_fins": 5,
      "fin_t_mm": 1.2, "fin_h_mm": 8.0, "theta_sa_c_per_w": 27.0,
      "theta_sa_forced_c_per_w": 11.0, "mass_g": 3.5, "package": "BGA/QFP"},
     1.10),
    ("HS-BGA-19", "19x19x11 mm adhesive BGA heatsink, plain finned aluminum",
     {"l_mm": 19.0, "w_mm": 19.0, "h_mm": 11.0, "base_t_mm": 2.0, "n_fins": 6,
      "fin_t_mm": 1.2, "fin_h_mm": 9.0, "theta_sa_c_per_w": 19.0,
      "theta_sa_forced_c_per_w": 8.0, "mass_g": 6.0, "package": "BGA/QFP"},
     1.40),
    ("HS-BGA-27", "27x27x12 mm adhesive BGA heatsink, plain finned aluminum",
     {"l_mm": 27.0, "w_mm": 27.0, "h_mm": 12.0, "base_t_mm": 2.5, "n_fins": 8,
      "fin_t_mm": 1.2, "fin_h_mm": 9.5, "theta_sa_c_per_w": 13.0,
      "theta_sa_forced_c_per_w": 5.5, "mass_g": 12.0, "package": "BGA/QFP"},
     1.80),
    ("HS-EXTR-35SQ", "35x35x15 mm square extruded heatsink, plain finned",
     {"l_mm": 35.0, "w_mm": 35.0, "h_mm": 15.0, "base_t_mm": 3.0, "n_fins": 9,
      "fin_t_mm": 1.3, "fin_h_mm": 12.0, "theta_sa_c_per_w": 8.5,
      "theta_sa_forced_c_per_w": 3.6, "mass_g": 26.0, "package": "universal"},
     2.20),
    ("HS-EXTR-40SQ-20", "40x40x20 mm square extruded heatsink, plain finned",
     {"l_mm": 40.0, "w_mm": 40.0, "h_mm": 20.0, "base_t_mm": 3.0, "n_fins": 10,
      "fin_t_mm": 1.3, "fin_h_mm": 17.0, "theta_sa_c_per_w": 6.0,
      "theta_sa_forced_c_per_w": 2.5, "mass_g": 42.0, "package": "universal"},
     2.90),
    ("HS-EXTR-50SQ-25", "50x50x25 mm square extruded heatsink, plain finned",
     {"l_mm": 50.0, "w_mm": 50.0, "h_mm": 25.0, "base_t_mm": 4.0, "n_fins": 11,
      "fin_t_mm": 1.5, "fin_h_mm": 21.0, "theta_sa_c_per_w": 4.2,
      "theta_sa_forced_c_per_w": 1.8, "mass_g": 80.0, "package": "universal"},
     4.50),
    ("HS-EXTR-60X40", "60x40x25 mm extruded heatsink, plain finned aluminum",
     {"l_mm": 60.0, "w_mm": 40.0, "h_mm": 25.0, "base_t_mm": 4.0, "n_fins": 10,
      "fin_t_mm": 1.5, "fin_h_mm": 21.0, "theta_sa_c_per_w": 3.6,
      "theta_sa_forced_c_per_w": 1.5, "mass_g": 92.0, "package": "universal"},
     5.20),
    ("HS-EXTR-100X69", "100x69x36 mm extruded heatsink, plain finned aluminum",
     {"l_mm": 100.0, "w_mm": 69.0, "h_mm": 36.0, "base_t_mm": 5.0, "n_fins": 12,
      "fin_t_mm": 2.0, "fin_h_mm": 31.0, "theta_sa_c_per_w": 1.6,
      "theta_sa_forced_c_per_w": 0.7, "mass_g": 320.0, "package": "universal"},
     11.00),
]


def _make_record(designation: str, title: str, params: dict, cost_usd) -> dict:
    p = dict(params)
    p["material"] = "aluminum"
    if cost_usd is not None:
        p["cost_usd"] = cost_usd
    return {
        "part_id":      f"{_DOMAIN}:heatsink:{designation}",
        "domain":       _DOMAIN,
        "title":        f"{title} heatsink heat sink",
        "standard":     "",
        "designation":  designation,
        "params":       p,
        "geometry_ref": {"kind": "parametric_heatsink",
                         "generator": "services.mcp.heatsink.sink_ops",
                         "spec": {k: p.get(k) for k in
                                  ("l_mm", "w_mm", "base_t_mm", "n_fins",
                                   "fin_t_mm", "fin_h_mm")}},
        "sourcing":     [],
        "provenance":   ("Curated engineering nominal for a common commercial "
                         "plain-finned aluminum heatsink family (heatsink selection "
                         "loop). theta_sa values are datasheet-class NATURAL-"
                         "convection nominals — verify the exact part's datasheet "
                         "before release."),
        "license":      "curated-nominal",
    }


class HeatsinksAdapter:
    """CatalogProvider for the curated heatsink catalog (domain "thermal")."""

    domain: str = _DOMAIN
    provider_id: str = "heatsinks"

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
        return len(_CATALOG)

    def iter_records(self) -> Iterator[dict]:
        for designation, title, params, cost in _CATALOG:
            yield _make_record(designation, title, params, cost)
