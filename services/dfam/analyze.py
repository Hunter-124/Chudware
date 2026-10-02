"""Compose deterministic DFAM rows in the requested build frame.

Only requested rows are returned; geometric intermediates are shared. Estimates
are a finite-volume ranking model, never an assertion about a real toolpath.
SLS powder supports the part; SLA has no FDM bridge or warp assumptions.
"""
from __future__ import annotations
import numpy as np
from .mesh import PrintMesh
from .params import PrintParams
from .geometry import BuildGeometry
from . import checks as algorithms
from .checks import CHECK_NAMES, CheckContext, estimate, rounded, row, support_data


def analyze_printability(mesh: PrintMesh, params: PrintParams, build_dir=(0, 0, 1), *,
                         yaw_deg=0.0, checks: list[str] | None = None, detail=False) -> dict:
    names = list(CHECK_NAMES) if checks is None else list(dict.fromkeys(checks))
    unknown = set(names) - set(CHECK_NAMES)
    if unknown:
        raise ValueError(f"unknown print checks: {', '.join(sorted(unknown))}")
    geom = BuildGeometry(mesh, build_dir, yaw_deg)
    ctx = CheckContext(geom, params, bool(detail))
    direction = np.asarray(build_dir, dtype=float)
    direction /= np.linalg.norm(direction)
    rows = []
    for name in names:
        if params.process not in ("fdm", "sla", "sls"):
            result = row(name, "not_applicable", f"DFAM check is not applicable to process {params.process}.")
        elif params.process == "sls" and name in ("overhang", "support", "bridges"):
            result = row(name, "not_applicable", "SLS powder supports the part; no overhang/bridge support check applies.")
        elif params.process == "sla" and name == "bridges":
            result = row(name, "not_applicable", "SLA does not use the FDM bridging assumption; ceilings need supports.")
        else:
            result = getattr(algorithms, name)(ctx)
        rows.append(result)
    statuses = {r["status"] for r in rows}
    status = ("fail" if "fail" in statuses else "not_evaluated" if "not_evaluated" in statuses else
              "warn" if "warn" in statuses else "pass")
    support_volume = support_data(ctx)["volume"] if params.process in ("fdm", "sla") else 0.
    return rounded({"build_dir": direction, "yaw_deg": float(yaw_deg), "rotation": geom.rotation,
                    "status": status, "checks": rows, "estimate": estimate(ctx, support_volume),
                    "frame": {"height_mm": float(geom.bounds[1, 2]),
                              "footprint_mm": geom.bounds[1, :2]-geom.bounds[0, :2]}})
