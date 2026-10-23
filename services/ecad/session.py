"""services.ecad.session — compat shim over the C++-backed session facade.

The session machinery (doc registry, apply_ops, undo/revisions, compile fields)
now lives in the C++ core behind RPC; :class:`services.mcp.ecad_common._SessionFacade`
mirrors the old ``engine.ecad.session`` API. This module re-exports that facade so
in-process consumers (autosave, tests) keep their ``session.new_session(...)`` shape.
"""
from __future__ import annotations

from services.mcp.ecad_common import EcadSession, OpError, ecad_session as _facade

new_session = _facade.new_session
get_session = _facade.get_session
iter_sessions = _facade.iter_sessions
apply_ops = _facade.apply_ops
rebuild = _facade.rebuild
tree = _facade.tree
swap_doc = _facade.swap_doc
close_session = _facade.close_session


# engine.ecad.session._REGISTRY compat: tests do ``session._REGISTRY.clear()``.
# Bind the live dict (mutated in place), not a copy.
_REGISTRY = _facade._REGISTRY

__all__ = [
    "EcadSession", "OpError", "new_session", "get_session", "iter_sessions",
    "apply_ops", "rebuild", "tree", "swap_doc", "close_session", "_REGISTRY",
]
