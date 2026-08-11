"""Unit tests for services/browser/driver.py — backend factory selection.

No network, no real browser launch. ImportError degrade paths are mocked so
CI / bare venvs stay green without patchright/camoufox installed.
"""
from __future__ import annotations

import importlib
import sys
import types
from typing import Any

import pytest


@pytest.fixture()
def driver_mod(monkeypatch):
    """driver module with a clean CHUDWARE_PW_BACKEND env each test.

    selected_backend() / resolve_* read the env at call time, so no reload is
    required (and popping sys.modules races the parent package attribute cache).
    """
    monkeypatch.delenv("CHUDWARE_PW_BACKEND", raising=False)
    from services.browser import driver as d
    yield d


# ---------------------------------------------------------------------------
# Selection logic
# ---------------------------------------------------------------------------


def test_default_backend_is_cef_when_env_unset(driver_mod, monkeypatch):
    monkeypatch.delenv("CHUDWARE_PW_BACKEND", raising=False)
    assert driver_mod.selected_backend() == "cef"
    assert driver_mod.DEFAULT_BACKEND == "cef"

def test_platform_forced_headless_defaults(driver_mod, monkeypatch):
    monkeypatch.delenv("CHUDWARE_PW_BACKEND", raising=False)
    assert driver_mod.selected_backend(platform="linux") == "cef"
    assert driver_mod.headless_backend(platform="linux") == "cef"
    assert driver_mod.selected_backend(platform="darwin") == "playwright"
    assert driver_mod.headless_backend(platform="darwin") == "playwright"


def test_backend_env_playwright_routes_headless_on_linux(driver_mod, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PW_BACKEND", "playwright")
    assert driver_mod.selected_backend(platform="linux") == "playwright"
    assert driver_mod.headless_backend(platform="linux") == "playwright"


def test_backend_env_patchright_routes_headless(driver_mod, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PW_BACKEND", "patchright")
    assert driver_mod.headless_backend(platform="linux") == "patchright"



def test_backend_env_patchright(driver_mod, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PW_BACKEND", "patchright")
    assert driver_mod.selected_backend() == "patchright"


def test_backend_env_camoufox(driver_mod, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PW_BACKEND", "camoufox")
    assert driver_mod.selected_backend() == "camoufox"


def test_backend_env_cef_explicit(driver_mod, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PW_BACKEND", "cef")
    assert driver_mod.selected_backend() == "cef"


def test_backend_env_unknown_falls_back_to_cef(driver_mod, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PW_BACKEND", "zendriver")
    assert driver_mod.selected_backend() == "cef"


def test_backend_env_case_and_whitespace(driver_mod, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PW_BACKEND", "  PatchRight  ")
    assert driver_mod.selected_backend() == "patchright"


# ---------------------------------------------------------------------------
# resolve_sync / resolve_async selection + ImportError degrade
# ---------------------------------------------------------------------------


def _install_fake_module(monkeypatch, full_name: str, attrs: dict[str, Any] | None = None):
    """Register a lightweight fake package/module under *full_name*."""
    parts = full_name.split(".")
    # Ensure parent packages exist so `from x.y import z` works.
    for i in range(1, len(parts) + 1):
        name = ".".join(parts[:i])
        if name not in sys.modules:
            mod = types.ModuleType(name)
            monkeypatch.setitem(sys.modules, name, mod)
            if i > 1:
                parent = sys.modules[".".join(parts[: i - 1])]
                setattr(parent, parts[i - 1], mod)
    mod = sys.modules[full_name]
    for k, v in (attrs or {}).items():
        setattr(mod, k, v)
    return mod


def test_resolve_sync_default_uses_playwright(driver_mod, monkeypatch):
    """Default (cef) residual path imports stock playwright.sync_api."""
    monkeypatch.delenv("CHUDWARE_PW_BACKEND", raising=False)
    sentinel = object()

    def _fake_sync():
        return sentinel

    _install_fake_module(
        monkeypatch, "playwright.sync_api", {"sync_playwright": _fake_sync}
    )
    # Also need top-level playwright present for package structure.
    _install_fake_module(monkeypatch, "playwright")
    got = driver_mod.resolve_sync()
    assert got is _fake_sync


def test_resolve_async_default_uses_playwright(driver_mod, monkeypatch):
    monkeypatch.delenv("CHUDWARE_PW_BACKEND", raising=False)

    def _fake_async():
        return "async-pw"

    _install_fake_module(
        monkeypatch, "playwright.async_api", {"async_playwright": _fake_async}
    )
    _install_fake_module(monkeypatch, "playwright")
    got = driver_mod.resolve_async()
    assert got is _fake_async


def test_resolve_sync_patchright_when_selected(driver_mod, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PW_BACKEND", "patchright")

    def _fake_pr_sync():
        return "pr-sync"

    _install_fake_module(
        monkeypatch, "patchright.sync_api", {"sync_playwright": _fake_pr_sync}
    )
    _install_fake_module(monkeypatch, "patchright")
    got = driver_mod.resolve_sync()
    assert got is _fake_pr_sync


def test_resolve_async_patchright_when_selected(driver_mod, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PW_BACKEND", "patchright")

    def _fake_pr_async():
        return "pr-async"

    _install_fake_module(
        monkeypatch, "patchright.async_api", {"async_playwright": _fake_pr_async}
    )
    _install_fake_module(monkeypatch, "patchright")
    got = driver_mod.resolve_async()
    assert got is _fake_pr_async


def test_resolve_sync_playwright_missing_raises_runtimeerror(driver_mod, monkeypatch):
    """ImportError of stock playwright degrades to RuntimeError (session shape)."""
    monkeypatch.delenv("CHUDWARE_PW_BACKEND", raising=False)
    # Ensure a failed import: remove any real/fake playwright modules and
    # install a meta_path finder that raises ImportError for playwright*.
    for key in list(sys.modules):
        if key == "playwright" or key.startswith("playwright."):
            monkeypatch.delitem(sys.modules, key, raising=False)

    class _BlockPlaywright:
        def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
            if fullname == "playwright" or fullname.startswith("playwright."):
                raise ImportError(f"No module named {fullname!r}")
            return None

    monkeypatch.setattr(sys, "meta_path", [_BlockPlaywright(), *sys.meta_path])
    with pytest.raises(RuntimeError) as ei:
        driver_mod.resolve_sync()
    msg = str(ei.value).lower()
    assert "playwright" in msg
    assert "not installed" in msg


def test_resolve_sync_patchright_missing_raises_runtimeerror(driver_mod, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PW_BACKEND", "patchright")
    for key in list(sys.modules):
        if key == "patchright" or key.startswith("patchright."):
            monkeypatch.delitem(sys.modules, key, raising=False)

    class _BlockPatchright:
        def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
            if fullname == "patchright" or fullname.startswith("patchright."):
                raise ImportError(f"No module named {fullname!r}")
            return None

    monkeypatch.setattr(sys, "meta_path", [_BlockPatchright(), *sys.meta_path])
    with pytest.raises(RuntimeError) as ei:
        driver_mod.resolve_sync()
    msg = str(ei.value).lower()
    assert "patchright" in msg
    assert "not installed" in msg


def test_resolve_async_patchright_missing_raises_runtimeerror(driver_mod, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PW_BACKEND", "patchright")
    for key in list(sys.modules):
        if key == "patchright" or key.startswith("patchright."):
            monkeypatch.delitem(sys.modules, key, raising=False)

    class _BlockPatchright:
        def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
            if fullname == "patchright" or fullname.startswith("patchright."):
                raise ImportError(f"No module named {fullname!r}")
            return None

    monkeypatch.setattr(sys, "meta_path", [_BlockPatchright(), *sys.meta_path])
    with pytest.raises(RuntimeError) as ei:
        driver_mod.resolve_async()
    assert "patchright" in str(ei.value).lower()


def test_resolve_sync_camoufox_missing_raises_runtimeerror(driver_mod, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PW_BACKEND", "camoufox")
    for key in list(sys.modules):
        if key == "camoufox" or key.startswith("camoufox."):
            monkeypatch.delitem(sys.modules, key, raising=False)

    class _BlockCamoufox:
        def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
            if fullname == "camoufox" or fullname.startswith("camoufox."):
                raise ImportError(f"No module named {fullname!r}")
            return None

    monkeypatch.setattr(sys, "meta_path", [_BlockCamoufox(), *sys.meta_path])
    with pytest.raises(RuntimeError) as ei:
        driver_mod.resolve_sync()
    assert "camoufox" in str(ei.value).lower()
    assert "not installed" in str(ei.value).lower()


def test_resolve_sync_camoufox_present_not_dropin(driver_mod, monkeypatch):
    """Camoufox imports but is not a sync_playwright drop-in — clear RuntimeError."""
    monkeypatch.setenv("CHUDWARE_PW_BACKEND", "camoufox")
    _install_fake_module(monkeypatch, "camoufox")
    with pytest.raises(RuntimeError) as ei:
        driver_mod.resolve_sync()
    msg = str(ei.value).lower()
    assert "camoufox" in msg
    assert "drop-in" in msg or "not a drop-in" in msg


# ---------------------------------------------------------------------------
# backend_available / provision (no network)
# ---------------------------------------------------------------------------


def test_backend_available_cef_always_true(driver_mod):
    assert driver_mod.backend_available("cef") is True


def test_backend_available_patchright_missing(driver_mod, monkeypatch):
    for key in list(sys.modules):
        if key == "patchright" or key.startswith("patchright."):
            monkeypatch.delitem(sys.modules, key, raising=False)

    class _Block:
        def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
            if fullname == "patchright" or fullname.startswith("patchright."):
                raise ImportError(f"No module named {fullname!r}")
            return None

    monkeypatch.setattr(sys, "meta_path", [_Block(), *sys.meta_path])
    assert driver_mod.backend_available("patchright") is False


def test_backend_available_patchright_present(driver_mod, monkeypatch):
    _install_fake_module(monkeypatch, "patchright")
    assert driver_mod.backend_available("patchright") is True


def test_backend_available_camoufox_present(driver_mod, monkeypatch):
    _install_fake_module(monkeypatch, "camoufox")
    assert driver_mod.backend_available("camoufox") is True


def test_backend_available_unknown_false(driver_mod):
    assert driver_mod.backend_available("zendriver") is False


def test_provision_status_offline_no_network(driver_mod):
    st = driver_mod.provision_status("patchright")
    assert st["name"] == "patchright"
    assert "available" in st
    assert st["mode"] == "cli"
    assert st.get("license") == "Apache-2.0"


def test_provision_browser_status_only_never_runs_cmd(driver_mod, monkeypatch):
    """install=False is pure status — must not invoke subprocess."""
    calls: list[Any] = []

    def _boom(*_a, **_k):
        calls.append(1)
        raise AssertionError("subprocess must not run on status-only provision")

    monkeypatch.setattr(driver_mod.subprocess, "run", _boom)
    res = driver_mod.provision_browser("patchright", install=False)
    assert res["action"] == "status_only"
    assert res["installed"] is False
    assert calls == []


def test_provision_browser_cef_install_is_noop(driver_mod, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("cef must never fetch via this module")

    monkeypatch.setattr(driver_mod.subprocess, "run", _boom)
    res = driver_mod.provision_browser("cef", install=True)
    assert res["action"] == "skipped_env_mode"
    assert res["ok"] is True
    assert res["installed"] is False


def test_provision_browser_unknown(driver_mod):
    res = driver_mod.provision_browser("zendriver", install=False)
    assert res["ok"] is False
    assert res["reason"] == "unknown_backend"


def test_provision_browser_install_package_missing(driver_mod, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PW_BACKEND", "patchright")
    for key in list(sys.modules):
        if key == "patchright" or key.startswith("patchright."):
            monkeypatch.delitem(sys.modules, key, raising=False)

    class _Block:
        def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
            if fullname == "patchright" or fullname.startswith("patchright."):
                raise ImportError(f"No module named {fullname!r}")
            return None

    monkeypatch.setattr(sys, "meta_path", [_Block(), *sys.meta_path])

    def _boom(*_a, **_k):
        raise AssertionError("must not run browser CLI without package")

    monkeypatch.setattr(driver_mod.subprocess, "run", _boom)
    res = driver_mod.provision_browser("patchright", install=True)
    assert res["ok"] is False
    assert res["reason"] == "package_not_installed"
    assert res["action"] == "package_missing"


def test_provision_browser_install_runs_descriptor_cmd(driver_mod, monkeypatch):
    """With package present, install=True runs the descriptor browser_cmd."""
    _install_fake_module(monkeypatch, "patchright")
    seen: dict[str, Any] = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(driver_mod.subprocess, "run", _run)
    res = driver_mod.provision_browser("patchright", install=True)
    assert res["ok"] is True
    assert res["installed"] is True
    assert res["action"] == "browser_cmd"
    assert seen["cmd"][:3] == [sys.executable, "-m", "patchright"]
    assert "install" in seen["cmd"]
    assert "chromium" in seen["cmd"]


def test_descriptors_schema_valid(driver_mod):
    for name, d in driver_mod.BROWSER_DESCRIPTORS.items():
        assert "kind" in d, name
        assert "license" in d, name
        assert "mode" in d, name
        assert d["mode"] in ("cli", "env", "download"), name
        if d["mode"] == "cli":
            assert d.get("browser_cmd"), name
            assert d.get("pip"), name
        if d["mode"] == "env":
            assert d.get("url") is None, name


def test_describe_backends_shape(driver_mod, monkeypatch):
    monkeypatch.delenv("CHUDWARE_PW_BACKEND", raising=False)
    info = driver_mod.describe_backends()
    assert info["selected"] == "cef"
    assert info["default"] == "cef"
    assert info["env_key"] == "CHUDWARE_PW_BACKEND"
    assert "cef" in info["backends"]
    assert "patchright" in info["backends"]
    assert "camoufox" in info["backends"]


# ---------------------------------------------------------------------------
# Seam wiring: session/embed import helpers go through driver
# ---------------------------------------------------------------------------


def test_session_import_seam_uses_driver(monkeypatch):
    monkeypatch.delenv("CHUDWARE_PW_BACKEND", raising=False)
    from services.browser import driver as d
    from services.browser import session

    sentinel = object()

    def _fake_resolve():
        return sentinel

    monkeypatch.setattr(d, "resolve_sync", _fake_resolve)
    assert session._import_sync_playwright() is sentinel


def test_embed_import_seam_uses_driver(monkeypatch):
    monkeypatch.delenv("CHUDWARE_PW_BACKEND", raising=False)
    from services.browser import driver as d
    from services.browser.embed import EmbedManager

    sentinel = object()

    def _fake_resolve():
        return sentinel

    monkeypatch.setattr(d, "resolve_async", _fake_resolve)
    mgr = EmbedManager()  # no injected factory
    assert mgr._import_async_playwright() is sentinel


def test_embed_injected_factory_bypasses_driver(monkeypatch):
    """Test injection seam still wins over driver.resolve_async."""
    from services.browser import driver as d
    from services.browser.embed import EmbedManager

    def _should_not_run():
        raise AssertionError("driver must not be consulted when injected")

    monkeypatch.setattr(d, "resolve_async", _should_not_run)
    injected = object()
    mgr = EmbedManager(_async_playwright=lambda: injected)
    assert mgr._import_async_playwright()() is injected
