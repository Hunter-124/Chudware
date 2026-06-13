"""Feature compilers for the document compiler (engine/doc/compiler.py).

Importing this package registers every available feature compiler (each module decorates its
entry points with ``@register(<type>)`` at import time). The compiler core imports this package
and looks compilers up via :func:`base.get_compiler`.

C09 adds dress/extrude/revolve modules here and registers them on the same registry; C10/C11
build on the :class:`CompileResult` the core returns. New compiler modules MUST be imported below
so their decorators run.
"""

from __future__ import annotations

from .base import (
    CompileContext,
    CompileOutput,
    Datum,
    FeatureError,
    get_compiler,
    register,
    registered_types,
)

# Import for side effects: each module registers its compilers on import.
from . import datum as _datum  # noqa: F401
from . import primitives as _primitives  # noqa: F401
from . import script as _script  # noqa: F401
# C09 registrations (dress-up, sketch, extrude, revolve):
from . import dress as _dress  # noqa: F401
from . import sketch as _sketch  # noqa: F401
from . import extrude as _extrude  # noqa: F401
from . import revolve as _revolve  # noqa: F401
# Sketched-feature sweep + loft (profile region(s) along a path / blended through sections):
from . import sweep as _sweep  # noqa: F401
from . import loft as _loft  # noqa: F401
# C21 registrations (import):
from . import import_feature as _import_feature  # noqa: F401
# C33-A registrations (hole wizard, multi-position):
from . import hole as _hole  # noqa: F401
# Patterns + mirror (replicate upstream seed features) and draft (taper faces):
from . import pattern as _pattern  # noqa: F401
from . import draft as _draft  # noqa: F401

__all__ = [
    "CompileContext",
    "CompileOutput",
    "Datum",
    "FeatureError",
    "get_compiler",
    "register",
    "registered_types",
]
