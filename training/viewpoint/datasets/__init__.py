"""ViewNet geometry-label datasets: fetch public CAD sources, map their face labels onto the
Chudware engine's faces by geometry, and emit ``chudware.geomlabel/1`` manifests
(contracts/geom_labels.md; source survey and licences in DATASETS.md).

Submodules are imported explicitly (``fetch``, ``importers``, ``build_manifest``); none of them
loads the engine or OCP at import time.
"""
