"""Root conftest.py — stub optional heavy deps ONLY when they are genuinely absent,
plus cross-test state hygiene (core_bridge daemon-fallback reset).

Optional vision and document-format dependencies may be absent in lightweight
test environments. Stubs support collection only when an import fails; installed
libraries must remain available to tests that exercise real image and file data.
"""
from __future__ import annotations

import sys
import types

# Repo root on sys.path so top-level packages (tools/, services/) are importable
# regardless of where pytest is invoked from (merged from the earlier root conftest).
import os
_ROOT = os.path.dirname(os.path.abspath(__file__))
_TEST_ROOTS = {
    "HOME": "home",
    "USERPROFILE": "home",
    "XDG_DATA_HOME": "data",
    "XDG_CONFIG_HOME": "config",
    "XDG_CACHE_HOME": "cache",
    "XDG_RUNTIME_DIR": "run",
    "CHUDWARE_CACHE_ROOT": "agent-cache",
    "CHUDWARE_SUBAGENTS_DIR": "subagents",
    "CHUDWARE_ECAD_LIB_DIR": "ecad",
    "CHUDWARE_HANDOFF_DIR": "handoff",
    "CHUDWARE_BOX_MIRROR_DIR": "box-mirror",
    "CHUDWARE_REVENG_DIR": "reveng",
    "CHUDWARE_MFG_STATE_DIR": "manufacturing",
}
_test_environment = None
_test_directory = None
_collection_engine_override = False
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)




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
# is file-backed under ~/.local/share/Chudware/cache/subagents by default, and
# DelegationTree.request_delegation registers a session on EVERY spawn — so any
# delegation-exercising test would pollute the real user registry (and the GUI's
# Sub-agents list) unless redirected. Session-scoped autouse: every test run gets
# a throwaway registry dir. An explicitly pre-set CHUDWARE_SUBAGENTS_DIR is honored.
import pytest as _pytest


@_pytest.fixture(autouse=True, scope="session")
def _isolated_subagent_registry(tmp_path_factory):
    if os.environ.get("CHUDWARE_SUBAGENTS_DIR"):
        yield
        return
    d = tmp_path_factory.mktemp("subagents")
    os.environ["CHUDWARE_SUBAGENTS_DIR"] = str(d)
    try:
        yield
    finally:
        os.environ.pop("CHUDWARE_SUBAGENTS_DIR", None)

# ---------------------------------------------------------------------------
# Generated-ECAD-part isolation. `ecad_make_part` writes symbols/footprints to
# services/ecad/library/generate.py::library_dir() — by default the REAL user library
# at ~/.local/share/Chudware/ecad_library — and `ecad_find_part` resolves through it.
#
# Two ways that bites, both observed 2026-07-30. A probe generated an 8-pin
# "Generated:ESP32_MINI" there, and from then on `ecad_find_part("ESP32")` answered with
# it — which silently disarmed the multi-pin substitution SAFETY refusal that
# test_library_refuses_multipin_substitution exists to pin, on that box, permanently.
# The mirror hazard is just as bad: a suite that writes into the real library changes
# what the user's own copilot finds afterwards.
#
# So the suite gets a throwaway library and asserts against the SHIPPED one, which is
# what those tests actually mean. An explicitly pre-set CHUDWARE_ECAD_LIB_DIR is honored
# (a caller who aimed it somewhere has already decided).
@_pytest.fixture(autouse=True, scope="session")
def _isolated_ecad_generated_library(tmp_path_factory):
    if os.environ.get("CHUDWARE_ECAD_LIB_DIR"):
        yield
        return
    d = tmp_path_factory.mktemp("ecad_library")
    os.environ["CHUDWARE_ECAD_LIB_DIR"] = str(d)
    try:
        yield
    finally:
        os.environ.pop("CHUDWARE_ECAD_LIB_DIR", None)

# ---------------------------------------------------------------------------
# Live-viewport handoff isolation. cad_common._publish_current writes
# `live-<session>-<doc>.step+` (+ docs.json) into the handoff dir on every doc mutation,
# and the desktop GUI POLLS that dir and opens a mirror tab per entry — so an unisolated
# suite run littered the developer's running app with parts nobody made.
#
# It also made test_live_preview_events_no_handoff_files order-dependent: that test
# snapshots the dir around its own body, but publishes can be DEFERRED onto a worker
# thread, so one scheduled by an earlier test lands inside the snapshot and trips
# "daemon mode must not touch the handoff dir". Observed 2026-07-30 once the suite grew
# enough documents to make the race reliable — services/mcp passed at 2399 tests and
# failed at 2492 with no change to the publishing code.
@_pytest.fixture(autouse=True, scope="session")
def _isolated_handoff_dir(tmp_path_factory):
    if os.environ.get("CHUDWARE_HANDOFF_DIR"):
        yield
        return
    d = tmp_path_factory.mktemp("handoff")
    os.environ["CHUDWARE_HANDOFF_DIR"] = str(d)
    try:
        yield
    finally:
        os.environ.pop("CHUDWARE_HANDOFF_DIR", None)


# ---------------------------------------------------------------------------
# Box mirror spool isolation. `Vault.save` calls box_mirror.on_vault_saved
# unconditionally, and that hook reads the REAL config.json — so on a box where the
# user has Box mirroring enabled, every vault-saving test enqueued its own throwaway
# tmp project into ~/.local/share/Chudware/box_mirror/spool. 2646 entries had piled
# up there (2633 from deleted `pytest-of-*` dirs), and since the agent turn's terminal
# sweep drained that spool, EVERY chat message paid the drain's full 90s budget
# re-walking dead entries — the "stuck on Thinking… for 120s" report.
#
# Worse in principle: a suite run was queueing uploads into the developer's real cloud
# account. Session-scoped autouse, same shape as the registry/handoff guards above; an
# explicitly pre-set CHUDWARE_BOX_MIRROR_DIR is honored.
@_pytest.fixture(autouse=True, scope="session")
def _isolated_box_mirror_spool(tmp_path_factory):
    if os.environ.get("CHUDWARE_BOX_MIRROR_DIR"):
        yield
        return
    d = tmp_path_factory.mktemp("box_mirror")
    os.environ["CHUDWARE_BOX_MIRROR_DIR"] = str(d)
    try:
        yield
    finally:
        os.environ.pop("CHUDWARE_BOX_MIRROR_DIR", None)


# ---------------------------------------------------------------------------
# Engine isolation for test sessions. `services/mcp/core_bridge` defaults to daemon mode
# (CHUDWARE_ENGINE_DAEMON unset == "1") and attaches to whatever chudware-hostd is
# already listening — on a developer box, the daemon behind the user's open Chudware
# window. Anything that mints a document then fans out `doc.opened`, the GUI opens a
# mirror tab for it, and the documents OUTLIVE the run, so the next run's
# new_part("SwingArm") collides with the stale one and gets a uniquified "SwingArm8" —
# the duplication compounding itself.
#
# That was fixed on 2026-07-28 in services/mcp/conftest.py and ONLY there. Every other
# suite — services/agent (3000+ tests), ecad, sim, parts, browser, tools — plus every
# bare script kept attaching to the user's live daemon. Measured 2026-07-29: a brand-new
# process saw 40 already-open FOREIGN documents on the default path and 0 with
# CHUDWARE_ENGINE_DAEMON=0. That is the mechanism behind the duplicate-part and
# "artifacts show whatever the user has open" reports.
#
# Why in-process (=0) here and NOT private_hostd.private_daemon(): a private daemon runs
# the ON-DISK chudware-hostd, which is a separate artifact built by `./build.sh daemon`
# and routinely older than the freshly relinked libchudware.so (observed 15:18 binary vs
# 17:35 engine). Hoisting the private daemon would therefore have traded a pollution bug
# for a STALE-ENGINE bug in every suite — the more expensive of the two, and the exact
# failure that scored six live agent sweeps against two-day-old code. Legacy in-process
# at the resolved CHUDWARE_LIBCHUDWARE is both cheaper (no spawn) and stricter (no shared
# pool can exist at all).
#
# services/mcp opts OUT because that suite asserts real daemon wire semantics
# (origin_kind, fanout, drain-replace) and must have a daemon; its conftest sets the
# sentinel below at import time, which is before any fixture runs. An explicit
# CHUDWARE_HOSTD_SOCKET or CHUDWARE_ENGINE_DAEMON always wins — that caller has already
# chosen what to talk to.
_WANTS_DAEMON_ENV = "CHUDWARE_TEST_WANTS_DAEMON"
_EXPLICIT_ENGINE_TARGET = ("CHUDWARE_ENGINE_DAEMON" in os.environ or
                           "CHUDWARE_HOSTD_SOCKET" in os.environ)


@_pytest.fixture(autouse=True, scope="session")
def _isolated_engine_session():
    if os.environ.get(_WANTS_DAEMON_ENV) == "1" \
            or os.environ.get("CHUDWARE_HOSTD_SOCKET") \
            or "CHUDWARE_ENGINE_DAEMON" in os.environ:
        yield False
        return
    os.environ["CHUDWARE_ENGINE_DAEMON"] = "0"
    try:
        yield True
    finally:
        os.environ.pop("CHUDWARE_ENGINE_DAEMON", None)


@_pytest.fixture(autouse=True)
def _isolated_engine_in_mixed_suite(request, _restore_path_configuration, monkeypatch):
    """Keep non-MCP tests off the user's daemon when MCP tests are collected too.

    services/mcp/conftest.py sets its daemon opt-out during collection, before
    the session fixture above runs. That opt-out must apply only to MCP tests:
    their private daemon starts at first use, not before preceding parts tests.
    """
    if os.environ.get(_WANTS_DAEMON_ENV) != "1" or _EXPLICIT_ENGINE_TARGET:
        yield
        return
    from pathlib import Path

    mcp_dir = Path(_ROOT) / "services" / "mcp"
    if Path(str(request.node.path)).resolve().is_relative_to(mcp_dir):
        yield
        return
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "0")
    yield

# ---------------------------------------------------------------------------
# `live` marker (Parts Library V2): tests that hit real vendor sites over the
# network under a saved browser profile. Skipped by default; opt in with
# `pytest --live` or CHUDWARE_LIVE_TESTS=1.

def pytest_addoption(parser):
    try:
        parser.addoption("--live", action="store_true", default=False,
                         help="run tests marked 'live' (real vendor sites, real network)")
    except ValueError:
        pass  # another plugin/conftest already registered --live


def pytest_configure(config):
    # Fixtures run after collection. Imports can already resolve paths, migrate a
    # legacy profile, or probe a backend, so isolate those roots before collection.
    import tempfile
    from pathlib import Path

    global _test_environment, _test_directory, _collection_engine_override
    _test_directory = tempfile.TemporaryDirectory(prefix="chudware-pytest-")
    root = Path(_test_directory.name)
    names = (*_TEST_ROOTS, "APPDATA", "CHUDWARE_TEST_WANTS_DAEMON")
    _test_environment = {name: os.environ.get(name) for name in names}
    os.environ.pop("APPDATA", None)
    os.environ.pop("CHUDWARE_TEST_WANTS_DAEMON", None)
    for name, suffix in _TEST_ROOTS.items():
        directory = root / suffix
        directory.mkdir(mode=0o700, exist_ok=True)
        os.environ[name] = str(directory)
    if not _EXPLICIT_ENGINE_TARGET:
        os.environ["CHUDWARE_ENGINE_DAEMON"] = "0"
        _collection_engine_override = True
    config.addinivalue_line(
        "markers",
        "live: hits real vendor sites over the network; opt in with --live or CHUDWARE_LIVE_TESTS=1",
    )


def pytest_collection_finish(session):
    global _collection_engine_override
    if _collection_engine_override:
        os.environ.pop("CHUDWARE_ENGINE_DAEMON", None)
        _collection_engine_override = False


def pytest_unconfigure(config):
    global _test_directory, _test_environment, _collection_engine_override
    if _collection_engine_override:
        os.environ.pop("CHUDWARE_ENGINE_DAEMON", None)
        _collection_engine_override = False
    if _test_environment is not None:
        for name, value in _test_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        _test_environment = None
    if _test_directory is not None:
        _test_directory.cleanup()
        _test_directory = None


def _live_enabled(config) -> bool:
    if os.environ.get("CHUDWARE_LIVE_TESTS") == "1":
        return True
    try:
        return bool(config.getoption("--live"))
    except (ValueError, KeyError):
        return False


def pytest_collection_modifyitems(config, items):
    if _live_enabled(config):
        return
    skip_live = _pytest.mark.skip(
        reason="live vendor test — opt in with --live or CHUDWARE_LIVE_TESTS=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@_pytest.fixture(autouse=True)
def _restore_path_configuration():
    """Keep reloads and dynamic-attribute patches local to their owning test.

    monkeypatch.setattr on paths.__getattr__ otherwise restores a concrete value,
    permanently shadowing actor-aware paths. Module reloads also replace the
    desktop root and can discard the MCP session's private CONFIG override.
    """
    paths = sys.modules.get("services.paths")
    saved_paths = {}
    if paths is not None:
        names = (*paths._PATH_SUFFIXES, "_DESKTOP_ROOT")
        saved_paths = {name: paths.__dict__[name] for name in names
                       if name in paths.__dict__}
    saved_environment = {name: os.environ.get(name) for name in _TEST_ROOTS}
    try:
        yield
    finally:
        if paths is not None:
            for name in names:
                if name in saved_paths:
                    paths.__dict__[name] = saved_paths[name]
                else:
                    paths.__dict__.pop(name, None)
        for name, value in saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        agent_config = sys.modules.get("services.agent.config")
        if agent_config is not None and "_cfg_cache" in agent_config.__dict__:
            with agent_config._cfg_cache_lock:
                agent_config._cfg_cache = None
                agent_config._cfg_cache_path = None
                agent_config._cfg_cache_mtime_ns = None


@_pytest.fixture(autouse=True)
def _reset_core_bridge_daemon_fallback():
    """core_bridge's daemon→legacy fallback is sticky for the process by design
    (docs/PERF_AUDIT_2026-07-10.md §1); in a pytest process that stickiness makes
    suite outcomes order-dependent (one intentionally-broken daemon strands every
    later test in legacy mode). Reset it on the canonical loaded module identity."""
    yield
    for name, mod in list(sys.modules.items()):
        if name.endswith("core_bridge") and mod is not None:
            reset = getattr(mod, "reset_daemon_fallback", None)
            if callable(reset):
                reset()
