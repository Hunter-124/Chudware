"""conftest.py for engine/vision tests.

Stubs out optional heavy dependencies (PIL/Pillow, cv2) before the package
__init__ fires, so tests that don't exercise those code paths can collect
and run without the real libraries installed.
"""
from __future__ import annotations

import sys
import types


def _stub_module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


# ---- PIL / Pillow stub (needed by engine.vision.ingest imported in __init__)
if "PIL" not in sys.modules:
    pil_stub = _stub_module("PIL")

    class _FakeImage:
        LANCZOS = 1

        @staticmethod
        def open(fp, *a, **kw):
            raise RuntimeError("PIL stub: Image.open not available in tests")

        @staticmethod
        def fromarray(*a, **kw):
            raise RuntimeError("PIL stub: not available")

    image_stub = _stub_module("PIL.Image", Image=_FakeImage, LANCZOS=1)
    image_stub.Image = _FakeImage
    image_stub.open = _FakeImage.open
    image_stub.LANCZOS = _FakeImage.LANCZOS
    pil_stub.Image = image_stub

    sys.modules["PIL"] = pil_stub
    sys.modules["PIL.Image"] = image_stub

# ---- cv2 stub (needed by engine.vision.fiducial imported in __init__)
if "cv2" not in sys.modules:
    cv2_stub = _stub_module("cv2")
    sys.modules["cv2"] = cv2_stub
