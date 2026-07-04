"""Root conftest.py — stub optional heavy deps ONLY when they are genuinely absent.

engine.vision.__init__ eagerly imports engine.vision.ingest, which needs Pillow (a declared
runtime dep). On a bare box without Pillow/cv2, collection of ANY test that touches engine
would die on that import — so we stub just enough for import-time. When the real library IS
installed we must NOT shadow it (T-F5's test_vision.py makes real photos with Pillow), hence
the try/except-import guard rather than a `sys.modules` presence check.
"""
from __future__ import annotations

import sys
import types

# Repo root on sys.path so top-level packages (tools/, services/, mcp_server/) are
# importable regardless of where pytest is invoked from (merged from the earlier root conftest).
import os
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# Windows compat: the repo exposes short top-level import names via git symlinks
# (engine->engine_oracle, agent->services/agent, dag->services/dag,
# deliverables->services/deliverables, installer->services/installer,
# mcp_server->services/mcp). On Windows checkouts with core.symlinks=false these
# materialize as plain text files, so `import engine.dfm` etc. fail at collection. Alias
# the short name to the real package in sys.modules so imports work regardless of
# filesystem symlink support. No-op where the real symlink already resolves (e.g. Linux),
# and silently skipped when a target is genuinely absent.
import importlib

_PKG_ALIASES = {
    "engine": "engine_oracle",
    "agent": "services.agent",
    "dag": "services.dag",
    "deliverables": "services.deliverables",
    "installer": "services.installer",
    "mcp_server": "services.mcp",
}
for _alias, _target in _PKG_ALIASES.items():
    if _alias in sys.modules:
        continue
    # On Linux the symlink resolves to a real directory, so normal import handles it and we
    # skip (zero overhead). On Windows (core.symlinks=false) the short name is a plain text
    # file — not a dir — so alias it to the real package. NOTE: this only fixes in-process
    # imports; child processes spawned as `python -m engine.<x>` still need real symlinks or
    # junctions on Windows (see docs/BUILD_WINDOWS.md).
    if os.path.isdir(os.path.join(_ROOT, _alias)):
        continue
    try:
        sys.modules[_alias] = importlib.import_module(_target)
    except ImportError:
        pass                                         # target absent — nothing to alias


def _stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


# PIL / Pillow — needed at import time by engine.vision.ingest.
try:
    import PIL.Image  # noqa: F401  (real Pillow wins whenever installed)
except ImportError:
    pil = _stub("PIL")

    class _FakeImage:
        LANCZOS = 1

        @staticmethod
        def open(*a, **kw):
            raise RuntimeError("PIL stub: Pillow is not installed in this test env")

        @staticmethod
        def fromarray(*a, **kw):
            raise RuntimeError("PIL stub: Pillow is not installed in this test env")

    img = _stub("PIL.Image", Image=_FakeImage, open=_FakeImage.open, LANCZOS=1)
    pil.Image = img
    sys.modules["PIL"] = pil
    sys.modules["PIL.Image"] = img

# cv2 — engine.vision.fiducial already guards its own import (HAVE_CV2), so no stub is
# needed; tests that need real cv2 use pytest.importorskip("cv2").
