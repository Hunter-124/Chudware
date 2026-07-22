"""Root conftest.py — stub optional heavy deps ONLY when they are genuinely absent,
plus cross-test state hygiene (core_bridge daemon-fallback reset).

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
# filesystem symlink support. NOTE: a bare `sys.modules['agent'] = services.agent`
# alias is NOT enough — on Linux the symlink makes `import agent.config` load
# config.py a SECOND time under the short name, so `agent.config` and
# `services.agent.config` become two distinct module objects. Monkeypatches then
# land on the copy the code under test doesn't read (order-dependent failures in
# test_tiers / test_sourcing_subagent / test_subagent_registry). The meta_path
# finder below makes the short name resolve to the SAME module object as the
# canonical path, for the package and every submodule, in both directions.
import importlib
import importlib.abc
import importlib.machinery

_PKG_ALIASES = {
    "engine": "engine_oracle",
    "agent": "services.agent",
    "dag": "services.dag",
    "deliverables": "services.deliverables",
    "installer": "services.installer",
    "mcp_server": "services.mcp",
}


class _AliasLoader(importlib.abc.Loader):
    """`import agent.x` returns the already-canonical `services.agent.x` module
    object (identity, not a re-execution of the same file)."""

    def __init__(self, target: str):
        self._target = target

    def create_module(self, spec):
        try:
            return importlib.import_module(self._target)
        except ImportError:
            return None  # target genuinely absent — fall back to default loading

    def exec_module(self, module):
        pass  # already executed by import_module above


class _SymlinkAliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        for alias, tgt in _PKG_ALIASES.items():
            if fullname == alias or fullname.startswith(alias + "."):
                real = tgt + fullname[len(alias):]
                return importlib.machinery.ModuleSpec(fullname, _AliasLoader(real))
        return None


sys.meta_path.insert(0, _SymlinkAliasFinder())
# Windows note: this also covers the core.symlinks=false checkout (the short
# name is a plain text file there), but only for IN-PROCESS imports; child
# processes spawned as `python -m engine.<x>` still need real symlinks or
# junctions on Windows (see docs/BUILD_WINDOWS.md).


def _stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


# ---------------------------------------------------------------------------
# Resource guard (benchmarks/simsuite/resguard.py): caps solver thread pools to
# the host's core budget and watchdogs the RSS of this test process + children
# (ccx/foam/ngspice) so a runaway solve aborts the run instead of freezing the
# box. Budgets scale with the machine (SIMSUITE_* env overrides); disable with
# SIMSUITE_GUARD=0. Must run before the engine/solver libs load their pools.
if os.environ.get("SIMSUITE_GUARD", "1") == "1":
    try:
        _RESGUARD_DIR = os.path.join(_ROOT, "benchmarks", "simsuite")
        if _RESGUARD_DIR not in sys.path:
            sys.path.insert(0, _RESGUARD_DIR)
        import resguard as _resguard
        _resguard.install()
    except Exception:
        pass  # the guard is protection, never a reason tests can't run


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


# ---------------------------------------------------------------------------
# Sub-agent registry isolation (Phase 4, M11): services/agent/subagent_registry.py
# is file-backed under ~/.local/share/AI-CAD/cache/subagents by default, and
# DelegationTree.request_delegation registers a session on EVERY spawn — so any
# delegation-exercising test would pollute the real user registry (and the GUI's
# Sub-agents list) unless redirected. Session-scoped autouse: every test run gets
# a throwaway registry dir. An explicitly pre-set AICAD_SUBAGENTS_DIR is honored.
import pytest as _pytest


@_pytest.fixture(autouse=True, scope="session")
def _isolated_subagent_registry(tmp_path_factory):
    if os.environ.get("AICAD_SUBAGENTS_DIR"):
        yield
        return
    d = tmp_path_factory.mktemp("subagents")
    os.environ["AICAD_SUBAGENTS_DIR"] = str(d)
    try:
        yield
    finally:
        os.environ.pop("AICAD_SUBAGENTS_DIR", None)


# ---------------------------------------------------------------------------
# `live` marker (Parts Library V2): tests that hit real vendor sites over the
# network under a saved browser profile. Skipped by default; opt in with
# `pytest --live` or AICAD_LIVE_TESTS=1.

def pytest_addoption(parser):
    try:
        parser.addoption("--live", action="store_true", default=False,
                         help="run tests marked 'live' (real vendor sites, real network)")
    except ValueError:
        pass  # another plugin/conftest already registered --live


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: hits real vendor sites over the network; opt in with --live or AICAD_LIVE_TESTS=1",
    )


def _live_enabled(config) -> bool:
    if os.environ.get("AICAD_LIVE_TESTS") == "1":
        return True
    try:
        return bool(config.getoption("--live"))
    except (ValueError, KeyError):
        return False


def pytest_collection_modifyitems(config, items):
    if _live_enabled(config):
        return
    skip_live = _pytest.mark.skip(
        reason="live vendor test — opt in with --live or AICAD_LIVE_TESTS=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@_pytest.fixture(autouse=True)
def _reset_core_bridge_daemon_fallback():
    """core_bridge's daemon→legacy fallback is sticky for the process by design
    (docs/PERF_AUDIT_2026-07-10.md §1); in a pytest process that stickiness makes
    suite outcomes order-dependent (one intentionally-broken daemon strands every
    later test in legacy mode). Reset it around each test, on every loaded module
    identity (mcp_server.core_bridge / services.mcp.core_bridge)."""
    yield
    for name, mod in list(sys.modules.items()):
        if name.endswith("core_bridge") and mod is not None:
            reset = getattr(mod, "reset_daemon_fallback", None)
            if callable(reset):
                reset()
