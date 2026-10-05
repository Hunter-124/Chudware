"""Schema validation + factories for ``aicad.piping/1`` documents.

Python executable form of ``contracts/piping_schema.md``: the kernel-side legs
(:mod:`services.piping.threed` / :mod:`services.piping.cfd_handoff`) share its constants and
their tests build/validate documents with it; the core's validator (``core/piping/schema.cpp``)
answers the ``piping_*`` RPCs. Structural validation only
(envelopes, types, id prefixes, enum membership) — geometry realizability and catalog
resolution are separate passes (:mod:`services.piping.catalog` / :mod:`services.piping.threed`),
just as the ECAD schema validator leaves DRC/3D to their own passes.

Every ``validate_*`` returns a list of error strings (empty = valid), each prefixed with a
JSON-path-ish locator so failures are actionable — same convention as the part-document and
ECAD schema validators.

Coordinates are ``[x, y, z]`` in **mm**; angles in **degrees**.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

SCHEMA_PIPING = "aicad.piping/1"

# ── enums (module constants so catalog.py / threed.py share one definition) ──────

SCHEDULES = {
    "5", "5S", "10", "10S", "20", "30", "40", "40S", "60", "80", "80S",
    "100", "120", "140", "160", "STD", "XS", "XXS",
}
FITTING_TYPES = {"elbow", "tee", "reducer", "flange"}
ELBOW_ANGLES = {45, 90}
ELBOW_RADII = {"LR", "SR"}
FLANGE_CLASSES = {150, 300, 600, 900, 1500, 2500}
FLANGE_FACES = {"RF", "FF", "RTJ"}
MATERIALS = {
    "A106-B", "A53-B", "A312-304", "A312-316", "A333-6", "PVC", "CPVC", "copper",
}
END_TYPES = {"BW", "SW", "THD", "FLG"}

# id prefix -> the document list key it indexes (used by next_id + validators)
_PREFIXES = {"r": "runs", "ft": "fittings"}


# ── factories ────────────────────────────────────────────────────────────────

def new_piping(name: str = "Piping") -> dict:
    return {
        "schema": SCHEMA_PIPING,
        "id": str(uuid.uuid4()),
        "name": name,
        "units": {"system": "MMGS"},
        "material": "A106-B",
        "runs": [],
        "fittings": [],
    }


def new_pipe_run(id: str, path: list, nps: float, schedule: str = "40",
                 name: str = "Run", **kw) -> dict:
    run = {
        "id": id,
        "name": name,
        "suppressed": False,
        "path": [list(map(float, p)) for p in path],
        "nps": nps,
        "schedule": schedule,
        "material": None,
        "bend_radius": None,
        "end_type": "BW",
        "bore_mm": None,
    }
    run.update({k: v for k, v in kw.items() if k in run})
    return run


def new_fitting(id: str, type: str, nps: float, origin=(0.0, 0.0, 0.0),
                schedule: str = "40", name: str = "Fitting", **kw) -> dict:
    ft = {
        "id": id,
        "type": type,
        "name": name,
        "suppressed": False,
        "origin": [float(c) for c in origin],
        "orient": [0.0, 0.0, 0.0],
        "nps": nps,
        "nps2": None,
        "schedule": schedule,
        "material": None,
        "catalog_ref": None,
        "params": {},
    }
    ft.update({k: v for k, v in kw.items() if k in ft})
    return ft


def is_piping(doc: dict) -> bool:
    return isinstance(doc, dict) and doc.get("schema") == SCHEMA_PIPING


def next_id(doc: dict, prefix: str) -> str:
    """Allocate the next unused ``<prefix><n>`` id (never reuses; scans current ids)."""
    key = _PREFIXES.get(prefix)
    items = doc.get(key, []) if key else []
    n = 0
    pat = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    for it in items:
        m = pat.match(it.get("id", "")) if isinstance(it, dict) else None
        if m:
            n = max(n, int(m.group(1)))
    return f"{prefix}{n + 1}"


# ── validation helpers ─────────────────────────────────────────────────────────

def _is_xyz(v: Any) -> bool:
    return (isinstance(v, (list, tuple)) and len(v) == 3
            and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in v))


def _check_ids(items, prefix: str, loc: str, errs: list[str]) -> None:
    seen = set()
    pat = re.compile(rf"^{re.escape(prefix)}\d+$")
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            errs.append(f"{loc}[{i}]: not an object")
            continue
        oid = it.get("id")
        if not isinstance(oid, str) or not pat.match(oid):
            errs.append(f"{loc}[{i}].id: missing or bad prefix (want {prefix}<n>) got {oid!r}")
        elif oid in seen:
            errs.append(f"{loc}[{i}].id: duplicate {oid!r}")
        else:
            seen.add(oid)


def _check_size(item: dict, loc: str, errs: list[str], *, key: str = "nps",
                required: bool = True) -> None:
    v = item.get(key)
    if v is None:
        if required:
            errs.append(f"{loc}.{key}: required")
        return
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        errs.append(f"{loc}.{key}: must be a positive number (inches), got {v!r}")


def _check_schedule(item: dict, loc: str, errs: list[str]) -> None:
    s = item.get("schedule")
    if s is not None and s not in SCHEDULES:
        errs.append(f"{loc}.schedule: {s!r} not in SCHEDULES")


def _check_material(item: dict, loc: str, errs: list[str]) -> None:
    m = item.get("material")
    if m is not None and m not in MATERIALS:
        errs.append(f"{loc}.material: {m!r} not in MATERIALS")


# ── validators ─────────────────────────────────────────────────────────────────

def validate_run(run: dict, loc: str = "run") -> list[str]:
    errs: list[str] = []
    if not isinstance(run, dict):
        return [f"{loc}: not an object"]
    path = run.get("path")
    if not isinstance(path, list) or len(path) < 2:
        errs.append(f"{loc}.path: need >= 2 points")
    else:
        for j, p in enumerate(path):
            if not _is_xyz(p):
                errs.append(f"{loc}.path[{j}]: expected [x,y,z] numbers, got {p!r}")
        # consecutive points must differ
        for j in range(len(path) - 1):
            a, b = path[j], path[j + 1]
            if _is_xyz(a) and _is_xyz(b) and a == list(b):
                errs.append(f"{loc}.path[{j}]: zero-length segment (duplicate point)")
    _check_size(run, loc, errs)
    _check_schedule(run, loc, errs)
    _check_material(run, loc, errs)
    et = run.get("end_type")
    if et is not None and et not in END_TYPES:
        errs.append(f"{loc}.end_type: {et!r} not in END_TYPES")
    br = run.get("bend_radius")
    if br is not None and (isinstance(br, bool) or not isinstance(br, (int, float)) or br <= 0):
        errs.append(f"{loc}.bend_radius: must be a positive number (mm) or null, got {br!r}")
    bore = run.get("bore_mm")
    if bore is not None and (isinstance(bore, bool)
                             or not isinstance(bore, (int, float)) or bore <= 0):
        errs.append(f"{loc}.bore_mm: must be a positive number (mm) or null, got {bore!r}")
    return errs


def validate_fitting(ft: dict, loc: str = "fitting") -> list[str]:
    errs: list[str] = []
    if not isinstance(ft, dict):
        return [f"{loc}: not an object"]
    ftype = ft.get("type")
    if ftype not in FITTING_TYPES:
        errs.append(f"{loc}.type: {ftype!r} not in FITTING_TYPES")
    if not _is_xyz(ft.get("origin")):
        errs.append(f"{loc}.origin: expected [x,y,z] numbers, got {ft.get('origin')!r}")
    if not _is_xyz(ft.get("orient")):
        errs.append(f"{loc}.orient: expected [x,y,z] degrees, got {ft.get('orient')!r}")
    _check_size(ft, loc, errs)
    _check_schedule(ft, loc, errs)
    _check_material(ft, loc, errs)

    if ftype == "reducer":
        _check_size(ft, loc, errs, key="nps2", required=True)
    elif ft.get("nps2") is not None:
        _check_size(ft, loc, errs, key="nps2", required=False)

    params = ft.get("params", {})
    if not isinstance(params, dict):
        errs.append(f"{loc}.params: must be an object")
        params = {}
    if ftype == "elbow":
        ang = params.get("angle")
        if ang is not None and ang not in ELBOW_ANGLES:
            errs.append(f"{loc}.params.angle: {ang!r} not in ELBOW_ANGLES")
        rad = params.get("radius")
        if rad is not None and rad not in ELBOW_RADII:
            errs.append(f"{loc}.params.radius: {rad!r} not in ELBOW_RADII")
    elif ftype == "flange":
        fc = params.get("flange_class")
        if fc is not None and fc not in FLANGE_CLASSES:
            errs.append(f"{loc}.params.flange_class: {fc!r} not in FLANGE_CLASSES")
        ff = params.get("flange_face")
        if ff is not None and ff not in FLANGE_FACES:
            errs.append(f"{loc}.params.flange_face: {ff!r} not in FLANGE_FACES")
    return errs


def validate_piping(doc: dict) -> list[str]:
    errs: list[str] = []
    if doc.get("schema") != SCHEMA_PIPING:
        return [f"schema: expected {SCHEMA_PIPING!r} got {doc.get('schema')!r}"]
    if not isinstance(doc.get("id"), str):
        errs.append("id: missing")
    _check_material(doc, "doc", errs)

    runs = doc.get("runs", [])
    fittings = doc.get("fittings", [])
    if not isinstance(runs, list):
        errs.append("runs: must be a list")
        runs = []
    if not isinstance(fittings, list):
        errs.append("fittings: must be a list")
        fittings = []
    _check_ids(runs, "r", "runs", errs)
    _check_ids(fittings, "ft", "fittings", errs)

    for i, run in enumerate(runs):
        errs.extend(validate_run(run, f"runs[{i}]"))
    for i, ft in enumerate(fittings):
        errs.extend(validate_fitting(ft, f"fittings[{i}]"))
    return errs


# Convenience alias mirroring services.ecad.schema.validate
def validate(doc: dict) -> list[str]:
    return validate_piping(doc)
