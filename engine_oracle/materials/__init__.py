"""engine/materials — the unified, provenance-stamped material registry (Pillar 8, T-F2).

One first-party fact table (``registry.json``) that the FEA sim path, the DfM density
defaults and the ``research.material_lookup`` helper all read, replacing three tables
that used to drift. See ``registry.py`` for the loader API and the legal posture.
"""
from __future__ import annotations

from .registry import (
    CAM_FIELDS,
    all_materials,
    cam_data,
    cam_material_id,
    cam_materials,
    density,
    fea_aliases,
    fea_table,
    get,
    registry_path,
    resolve_key,
)

__all__ = [
    "all_materials",
    "get",
    "resolve_key",
    "density",
    "fea_table",
    "fea_aliases",
    "registry_path",
    "cam_data",
    "cam_material_id",
    "cam_materials",
    "CAM_FIELDS",
]
