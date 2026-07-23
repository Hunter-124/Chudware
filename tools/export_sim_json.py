"""Generate ``engine/sim/materials.json`` from the unified registry (T-F2).

``engine/sim/materials.json`` is read at runtime by the desktop FEA GUI
(``host/src/ui/sim_manager.cpp`` -> ``loadMaterialsFile(".../engine/sim/materials.json")``)
keyed by a fixed set of *sim keys* that the appearance mapping also references. To make the
registry the single source of truth **without changing those keys**, this module re-emits
the same key set with the mechanical values pulled from ``engine.materials`` — so the C++
GUI and the Python subsystems share one reconciled table. ``test_export_sim_json`` asserts
the checked-in file equals this output, so the two can never drift again.

Run ``python -m engine.materials.export_sim_json`` (or call :func:`write`) after editing the
registry to refresh the mirror.
"""
from __future__ import annotations

import json
import pathlib
from typing import Iterable, Optional

from . import registry

_SIM_JSON = pathlib.Path(__file__).resolve().parents[1] / "sim" / "materials.json"
# The flat fields the C++ FEA study reads (host/src/ui/sim_manager.cpp).
_FIELDS = ("name", "E_mpa", "nu", "density_g_cm3", "yield_mpa", "ultimate_mpa", "endurance_mpa")


def sim_json_path() -> pathlib.Path:
    """Absolute path to the generated ``engine/sim/materials.json`` mirror."""
    return _SIM_JSON


def current_sim_keys() -> list:
    """The sim keys the checked-in ``materials.json`` currently defines (drives which
    registry rows are mirrored, so the C++ key contract is preserved)."""
    return list(json.loads(_SIM_JSON.read_text(encoding="utf-8")).keys())


def render(sim_keys: Optional[Iterable[str]] = None) -> dict:
    """Build the ``materials.json`` mapping ``{sim_key: {flat mechanical fields}}`` from
    the registry. ``sim_keys`` defaults to the existing file's keys (idempotent refresh)."""
    keys = list(sim_keys) if sim_keys is not None else current_sim_keys()
    out: dict[str, dict] = {}
    for k in keys:
        rec = registry.get(k)
        if rec is None:
            raise KeyError(f"sim key {k!r} does not resolve in engine.materials registry")
        mech = rec["mechanical"]
        row = {"name": rec["name"]}
        row.update({f: mech[f] for f in _FIELDS if f in mech})
        out[k] = row
    return out


def render_text(sim_keys: Optional[Iterable[str]] = None) -> str:
    """The exact serialized form written to disk (2-space indent + trailing newline)."""
    return json.dumps(render(sim_keys), indent=2, ensure_ascii=False) + "\n"


def write(sim_keys: Optional[Iterable[str]] = None) -> pathlib.Path:
    """Regenerate the mirror on disk from the registry; returns the path written."""
    _SIM_JSON.write_text(render_text(sim_keys), encoding="utf-8")
    return _SIM_JSON


if __name__ == "__main__":  # pragma: no cover
    p = write()
    print(f"wrote {p} ({len(current_sim_keys())} materials) from the unified registry")
