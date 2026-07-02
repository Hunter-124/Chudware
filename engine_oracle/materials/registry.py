"""Unified material registry — the single first-party source of material properties.

Pillar 8 (T-F2): before this module three tables drifted independently — the FEA
mechanical library (``services/mcp/cad_tools._MATERIALS``), the orphaned
``engine/sim/materials.json`` seed, and the DfM density defaults in
``engine/dfm/process_profiles`` — so a "6061" yield could disagree across sim, CAM and
DfM. They are unified here with **per-row provenance**; the sim (FEA) path, the DfM
density defaults and the ``material_lookup`` research helper all read this one table.

Legal (master plan §Pillar 8, ``contracts/licenses.md``): every row is **bundle-safe**
first-party engineering fact-table data (public handbook nominal values — ASM Metals
Reference, MMPDS/MIL-HDBK-5 nominal, USDA Wood Handbook, manufacturer public datasheets)
or CC-BY (Materials Project). **No MatWeb / MakeItFrom / Matmatch** content is ever
copied — those prohibit programmatic reuse. Values are *nominal engineering priors* for
first-pass sizing, not a certified allowables database.

Stdlib-only (``json`` + ``pathlib``); no engine/OCP imports so any layer can read it.
The JSON seed (~two dozen common alloys/plastics) is intentionally extensible — append
rows to ``registry.json`` following the same schema; nothing here is hard-coded.
"""
from __future__ import annotations

import copy
import functools
import json
import pathlib
from typing import Any, Optional

_DATA = pathlib.Path(__file__).with_name("registry.json")

# The mechanical fields the FEA study consumes (host/src/ui/sim_manager.cpp): E in MPa,
# Poisson nu, density g/cm^3, strengths in MPa.
_MECH_FIELDS = ("E_mpa", "nu", "density_g_cm3", "yield_mpa", "ultimate_mpa", "endurance_mpa")


@functools.lru_cache(maxsize=1)
def _doc() -> dict:
    """Parse and cache the JSON seed (immutable; callers get copies)."""
    return json.loads(_DATA.read_text(encoding="utf-8"))


def registry_path() -> pathlib.Path:
    """Absolute path to the bundled ``registry.json`` seed."""
    return _DATA


def all_materials() -> dict:
    """Every material record keyed by canonical id (fresh deep copy)."""
    return copy.deepcopy(_doc()["materials"])


@functools.lru_cache(maxsize=1)
def _alias_index() -> dict:
    """Lower-cased alias/name/key -> canonical key, built once from the seed."""
    idx: dict[str, str] = {}
    for key, rec in _doc()["materials"].items():
        idx[key.lower()] = key
        idx[str(rec.get("name", "")).strip().lower()] = key
        for a in rec.get("aliases", []):
            idx[str(a).strip().lower()] = key
    return idx


def resolve_key(name: str) -> Optional[str]:
    """Canonical registry key for a name/alias, or ``None`` if unknown."""
    if not name:
        return None
    return _alias_index().get(str(name).strip().lower())


def get(name: str) -> Optional[dict]:
    """Full record (mechanical + provenance) for a name/alias, fresh copy, else ``None``."""
    key = resolve_key(name)
    if key is None:
        return None
    rec = copy.deepcopy(_doc()["materials"][key])
    rec["key"] = key
    return rec


def density(name: str) -> Optional[float]:
    """``density_g_cm3`` for a material name/alias, or ``None`` if unknown."""
    rec = get(name)
    if rec is None:
        return None
    return rec.get("mechanical", {}).get("density_g_cm3")


# --------------------------------------------------------------------------- FEA seam
# The FEA path (cad_tools) historically owned a 7-entry ``_MATERIALS`` literal keyed by
# friendly names ("steel" = mild A36, not 1018). These helpers reproduce that exact shape
# from the registry so cad_tools drops its literal without any behaviour change.

def fea_table() -> dict:
    """The FEA mechanical library in the legacy ``_MATERIALS`` shape:
    ``{friendly_name: {"name", "E_mpa", "nu", "density_g_cm3", "yield_mpa",
    "ultimate_mpa", "endurance_mpa"}}``. Single source = the registry."""
    out: dict[str, dict] = {}
    for friendly, key in _doc()["fea_canonical"].items():
        rec = _doc()["materials"][key]
        mech = rec["mechanical"]
        row = {"name": rec["name"]}
        row.update({f: mech[f] for f in _MECH_FIELDS})
        out[friendly] = row
    return out


def fea_aliases() -> dict:
    """The legacy ``_MATERIAL_ALIASES`` map (alias -> friendly FEA key)."""
    return dict(_doc()["fea_aliases"])
