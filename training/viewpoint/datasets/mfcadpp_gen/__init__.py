"""MFCAD++ part generator, ported from https://gitlab.com/CAPP-SRC/mfcad2-dataset (MIT, see
LICENSE here) to OCP + numpy.

A part is a random stock block (10–50 mm per side) with a multiset of 3–9 of the 24 MFCAD++
machining features cut into it; every face carries the class index of the feature that made it
(the 25-class `feat_names` table, contracts/geom_labels.md §3.1) and — new in this port — the
feature instance that made it. Each seed is generated independently and deterministically:

    <out>/<seed>.step          STEP AP214, every ADVANCED_FACE named by its face id
    <out>/<seed>.labels.json   sidecar (schema SIDECAR_SCHEMA): per face id its class, instance,
                               surface type, area and centroid; the drawn and applied features
    <out>/<seed>.failed.json   instead of both when the seed produced no valid part

Changes from the original (upstream commit c171b76b):
- pythonocc-core -> OCP; numba kernels -> vectorised numpy (same branch logic).
- One random.Random(seed) per part instead of the global generator; edge/face lists in TopExp
  order (the original listed edges through a Python set, so its choices changed between runs).
- Combos: main.py enumerated every multiset of 3..9 classes and shuffled; here each seed draws
  one uniformly from that same set (sample_combo).
- Ray casts ignore hits at t <= 1e-6 mm (rounding noise on the ray's own face used to veto
  bounds and blind depths).
- A feature whose prism/fillet/chamfer raises, or whose result is not one valid solid
  (BRepCheck), is skipped like any other unplaceable feature (the original crashed, or kept
  invalid solids); a failed fillet edge no longer stays in the fillet maker for later edges.
- Labels are recorded per face id with instance ids, in a sidecar next to the STEP.

The package imports no OCP at import time; generator/features/occ need it, runner drives worker
processes (a crashing OCC call takes down one worker, not the run).
"""
from __future__ import annotations

from pathlib import Path

GENERATOR_VERSION = "mfcadpp_gen/1"
SIDECAR_SCHEMA = "chudware.viewnet.mfcadpp-gen/1"
UPSTREAM_URL = "https://gitlab.com/CAPP-SRC/mfcad2-dataset"
UPSTREAM_COMMIT = "c171b76bb6f7e2537fcfafe6324b64a07d5e98db"
LICENSE = "MIT (generator: CAPP-SRC/mfcad2-dataset, (c) 2025 Andrew Colligan; parts generated locally)"


def step_path(out_dir: Path, seed: int) -> Path:
    return Path(out_dir) / f"{seed}.step"


def sidecar_path(step: Path) -> Path:
    return Path(step).with_suffix(".labels.json")


def failed_path(out_dir: Path, seed: int) -> Path:
    return Path(out_dir) / f"{seed}.failed.json"


def default_dir() -> Path:
    """$CHUDWARE_VIEWNET_DATA/raw/mfcadpp_gen — the generated parts' cache."""
    from .. import fetch  # noqa: PLC0415
    return fetch.data_root() / "raw" / "mfcadpp_gen"
