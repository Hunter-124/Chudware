"""CatalogProvider adapter for piping fittings.

Wraps the bundled B16 fittings catalog in services/piping/catalog.py
(``services/piping/data/fittings.csv``). Pipes are NOT enumerated here —
this adapter covers fitting geometry only (elbow, tee, reducer, flange).

Domain: "fitting". geometry_ref.kind = "fitting_id".

Params carry the native CSV columns verbatim (nps, nps2, center_to_end, length,
flange_class, od, thickness, bolt_circle, bolt_holes, bolt_dia) plus common core
keys (nominal_size, standard).

Provenance / license: B16.9 / B16.5 dimensional facts (not ASME text);
see services/piping/catalog.py module docstring and contracts/licenses.md.
"""
from __future__ import annotations

import logging
from typing import Iterator

log = logging.getLogger(__name__)


class PipingFittingsAdapter:
    """CatalogProvider for B16 piping fittings from the bundled CSV catalog.

    Enumerates elbow / tee / reducer / flange rows. Each record's params dict
    carries the native CSV columns verbatim plus common-core keys.
    """

    domain: str = "fitting"
    provider_id: str = "piping_fittings"

    # ------------------------------------------------------------------
    # CatalogProvider protocol
    # ------------------------------------------------------------------

    def search(self, query: str, *,
               domain: str | None = None,
               filters: dict | None = None) -> list[dict]:
        if domain and domain != "fitting":
            return []
        norm = query.strip().lower()
        if not norm:
            return list(self.iter_records())
        results = []
        for rec in self.iter_records():
            title = (rec.get("title") or "").lower()
            designation = (rec.get("designation") or "").lower()
            if norm in title or norm in designation:
                results.append(rec)
        return results

    def get(self, part_id: str) -> dict | None:
        if not part_id or not part_id.startswith("fitting:"):
            return None
        rest = part_id[len("fitting:"):]
        for rec in self.iter_records():
            if rec.get("part_id") == part_id:
                return rec
            # Also match on the catalog_id portion
            if rec.get("part_id", "").split(":", 1)[-1] == rest:
                return rec
        return None

    def count(self) -> int:
        return sum(1 for _ in self.iter_records())

    def iter_records(self) -> Iterator[dict]:
        rows = _get_rows()
        for row in rows:
            rec = _make_record(row)
            if rec is not None:
                yield rec


# ---------------------------------------------------------------------------
# Data loading (lazy, one import)
# ---------------------------------------------------------------------------

def _get_rows() -> tuple:
    try:
        from services.piping.catalog import _load_rows  # re-homed from engine_oracle
        return _load_rows()
    except Exception as exc:  # noqa: BLE001
        log.debug("PipingFittingsAdapter: catalog unavailable: %s", exc)
        return ()


# ---------------------------------------------------------------------------
# Record builder
# ---------------------------------------------------------------------------

_FITTING_STANDARDS = {
    "elbow":   "ASME B16.9",
    "tee":     "ASME B16.9",
    "reducer": "ASME B16.9",
    "flange":  "ASME B16.5",
}


def _make_record(row: dict) -> dict | None:
    """Build a PartRecord from a fittings.csv row."""
    ftype = row.get("type")
    if not ftype:
        return None
    nps = row.get("nps")
    if nps is None:
        return None

    # Build a stable catalog_id
    nps_str = f"NPS{nps}"
    nps2 = row.get("nps2")
    fc = row.get("flange_class")
    if ftype == "reducer" and nps2 is not None:
        catalog_id = f"{ftype}:{nps_str}x{nps2}"
        designation = f"NPS {nps} × {nps2}"
    elif ftype == "flange" and fc is not None:
        catalog_id = f"{ftype}:{nps_str}:cl{int(fc)}"
        designation = f"NPS {nps} Class {int(fc)}"
    else:
        catalog_id = f"{ftype}:{nps_str}"
        designation = f"NPS {nps}"

    standard = _FITTING_STANDARDS.get(ftype, "ASME B16")
    title = f"{standard} {ftype.replace('_', ' ').title()} {designation} piping fitting"

    params: dict = {"nominal_size": designation, "standard": standard}
    # Carry native columns verbatim (skip None values)
    for k, v in row.items():
        if v is not None and k != "type":
            params[k] = v

    return {
        "part_id":      f"fitting:{catalog_id}",
        "domain":       "fitting",
        "title":        title,
        "standard":     standard,
        "designation":  designation,
        "params":       params,
        "geometry_ref": {
            "kind":       "fitting_id",
            "fitting_id": catalog_id,
        },
        "sourcing":     [],
        "provenance":   (
            f"ASME B16 fitting dimensions (clean-room); {standard} dimensional facts. "
            "Source: services/piping/data/fittings.csv — ASME text not bundled. "
            "See services/piping/catalog.py and contracts/licenses.md."
        ),
        "license":      "clean-room",
    }
