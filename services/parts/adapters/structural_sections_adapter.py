"""CatalogProvider adapter for structural sections.

Wraps engine_oracle/structural/sections.py + the clean-room data tables:
  - engine_oracle/data/aisc_shapes/aisc_shapes_curated.json  (AISC W/S/C sections)
  - engine_oracle/data/lumber_table.json                     (S4S dressed lumber)

Domain: "section". geometry_ref.kind = "section_profile".

Params are carried VERBATIM from the source JSON (mm / mm² / mm³ / mm⁴) plus a
small common core (nominal_size, standard) as required by contracts/parts.md §1.
No renaming of native keys.

Provenance / license: clean-room curated dimensional data; see
engine_oracle/data/aisc_shapes/aisc_shapes_curated.json _comment field and
contracts/licenses.md. AISC shapes: clean-room. Lumber: public-domain (ANSI/AWC NDS).
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

# Data files re-homed to services/structural/data/ with the structural modules.
_DATA = Path(__file__).resolve().parents[2] / "structural" / "data"
_AISC_PATH = _DATA / "aisc_shapes" / "aisc_shapes_curated.json"
_LUMBER_PATH = _DATA / "lumber_table.json"


@lru_cache(maxsize=1)
def _load_aisc() -> dict:
    try:
        with open(_AISC_PATH, encoding="utf-8") as fh:
            return json.load(fh)["shapes"]
    except Exception as exc:  # noqa: BLE001
        log.debug("StructuralSectionsAdapter: AISC data unavailable: %s", exc)
        return {}


@lru_cache(maxsize=1)
def _load_lumber() -> dict:
    try:
        with open(_LUMBER_PATH, encoding="utf-8") as fh:
            return json.load(fh)["lumber"]
    except Exception as exc:  # noqa: BLE001
        log.debug("StructuralSectionsAdapter: lumber data unavailable: %s", exc)
        return {}


class StructuralSectionsAdapter:
    """CatalogProvider for AISC steel sections + dressed lumber (domain='section').

    Each record's params dict carries the native dimensional properties verbatim
    (A, d, bf, tf, tw, Ix, Sx, … for AISC; b_actual_mm, d_actual_mm, A, Ix, Sx,
    Iy, Sy for lumber) plus common-core keys: nominal_size, standard.
    """

    domain: str = "section"
    provider_id: str = "structural_sections"

    # ------------------------------------------------------------------
    # CatalogProvider protocol
    # ------------------------------------------------------------------

    def search(self, query: str, *,
               domain: str | None = None,
               filters: dict | None = None) -> list[dict]:
        """Substring search over title + designation (fallback — index is primary)."""
        if domain and domain != "section":
            return []
        norm = query.strip().lower()
        if not norm:
            return list(self.iter_records())
        results = []
        for rec in self.iter_records():
            title = (rec.get("title") or "").lower()
            designation = (rec.get("designation") or "").lower()
            standard = (rec.get("standard") or "").lower()
            if norm in title or norm in designation or norm in standard:
                results.append(rec)
        return results

    def get(self, part_id: str) -> dict | None:
        if not part_id or not part_id.startswith("section:"):
            return None
        rest = part_id[len("section:"):]
        # rest = "<std>:<designation>"
        parts = rest.split(":", 1)
        if len(parts) != 2:
            return None
        std, designation = parts
        for rec in self.iter_records():
            if rec.get("standard") == std and rec.get("designation") == designation:
                return rec
        return None

    def count(self) -> int:
        return len(_load_aisc()) + len(_load_lumber())

    def iter_records(self) -> Iterator[dict]:
        # AISC steel sections
        for name, props in _load_aisc().items():
            yield _make_aisc_record(name, props)
        # Dressed lumber
        for nominal, props in _load_lumber().items():
            yield _make_lumber_record(nominal, props)


# ---------------------------------------------------------------------------
# Record builders
# ---------------------------------------------------------------------------

def _make_aisc_record(name: str, props: dict) -> dict:
    """Build a PartRecord for an AISC section."""
    # name is e.g. "W8X31" — normalise to "W8x31" for designation display
    designation = name.replace("X", "x")
    # Determine section type from name prefix for title
    prefix = name[0] if name else "?"
    section_types = {"W": "Wide Flange", "S": "S-Shape", "C": "Channel",
                     "L": "Angle", "HSS": "HSS", "HP": "HP Pile", "M": "Misc"}
    stype = section_types.get(prefix, "Section")
    title = f"AISC {stype} {designation} structural section"

    params: dict = {"nominal_size": designation, "standard": "AISC360"}
    # Carry native keys verbatim (A, d, bf, tf, tw, Ix, Sx, Zx, Iy, Sy, Zy, J, Cw)
    for k, v in props.items():
        if not k.startswith("_"):
            params[k] = v

    return {
        "part_id":      f"section:AISC360:{designation}",
        "domain":       "section",
        "title":        title,
        "standard":     "AISC360",
        "designation":  designation,
        "params":       params,
        "geometry_ref": {
            "kind":        "section_profile",
            "standard":    "AISC360",
            "designation": designation,
        },
        "sourcing":     [],
        "provenance":   (
            "AISC section properties from clean-room curated table; dimensional facts "
            "from public-domain structural engineering references (Salmon & Johnson, NSAI, "
            "CISC Handbook). See engine_oracle/data/aisc_shapes/aisc_shapes_curated.json "
            "and contracts/licenses.md."
        ),
        "license":      "clean-room",
    }


def _make_lumber_record(nominal: str, props: dict) -> dict:
    """Build a PartRecord for a dressed lumber size."""
    designation = nominal  # e.g. "2x4"
    title = f"Dressed lumber {designation} S4S structural section"

    params: dict = {"nominal_size": designation, "standard": "NDS"}
    # Carry native keys verbatim
    for k, v in props.items():
        params[k] = v

    return {
        "part_id":      f"section:NDS:{designation}",
        "domain":       "section",
        "title":        title,
        "standard":     "NDS",
        "designation":  designation,
        "params":       params,
        "geometry_ref": {
            "kind":        "section_profile",
            "standard":    "NDS",
            "designation": designation,
        },
        "sourcing":     [],
        "provenance":   (
            "Nominal-to-actual dressed (S4S) lumber dimensions per ANSI/AWC National Design "
            "Specification (NDS) and American Softwood Lumber Standard PS 20 — a public "
            "standard; values are physical facts. See engine_oracle/data/lumber_table.json "
            "and contracts/licenses.md."
        ),
        "license":      "public-domain",
    }
