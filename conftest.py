"""Root conftest.py — stubs optional heavy deps before any package __init__ fires.

This lets the vision/photogrammetry tests run without PIL, cv2, open3d installed.
"""
from __future__ import annotations

import sys
import types


def _stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


# PIL / Pillow — needed by engine.vision.ingest (imported eagerly in __init__)
if "PIL" not in sys.modules:
    pil = _stub("PIL")
    img = _stub("PIL.Image")

    class _FakeImage:
        LANCZOS = 1

        @staticmethod
        def open(*a, **kw):
            raise RuntimeError("PIL stub")

        @staticmethod
        def fromarray(*a, **kw):
            raise RuntimeError("PIL stub")

    img.Image = _FakeImage
    img.open = _FakeImage.open
    img.LANCZOS = _FakeImage.LANCZOS
    pil.Image = img
    sys.modules["PIL"] = pil
    sys.modules["PIL.Image"] = img

# cv2 — needed by engine.vision.fiducial (imported eagerly in __init__)
if "cv2" not in sys.modules:
    sys.modules["cv2"] = _stub("cv2")
