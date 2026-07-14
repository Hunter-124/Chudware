"""AI-CAD MCP server package (named mcp_server to avoid shadowing the `mcp` SDK).

``mcp_server`` is a filesystem symlink to ``services/mcp``, but Python keys modules by
import name — without the alias below both packages (and every submodule) load twice
with disjoint module-level state (``_ECAD``, ``_THERMAL``, timers, …). Both names are
forced onto ONE package object so ``import mcp_server.X`` and ``import services.mcp.X``
share identity.
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import sys as _sys
import types as _types


# Canonical name is mcp_server (what server.py and most tools use). services.mcp is the alias.
_CANON = "mcp_server"
_ALIAS = "services.mcp"
_PKG_NAMES = (_CANON, _ALIAS)


def _pair(fullname: str) -> tuple[str, str] | None:
    """Return (canonical_name, alias_name) for a dual-package fullname, else None."""
    if fullname == _CANON or fullname.startswith(_CANON + "."):
        return fullname, _ALIAS + fullname[len(_CANON):]
    if fullname == _ALIAS or fullname.startswith(_ALIAS + "."):
        return _CANON + fullname[len(_ALIAS):], fullname
    return None


def _unify_dual_package() -> None:
    """Alias mcp_server <-> services.mcp onto a single package + shared submodules."""
    here = _sys.modules[__name__]
    twin = None
    for n in _PKG_NAMES:
        m = _sys.modules.get(n)
        if m is not None and m is not here:
            twin = m
            break
    canon = twin if twin is not None else here
    try:
        path = list(getattr(canon, "__path__", None)
                    or getattr(here, "__path__", None)
                    or [])
        if path:
            canon.__path__ = path  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    for n in _PKG_NAMES:
        _sys.modules[n] = canon
    try:
        services_pkg = _sys.modules.get("services")
        if services_pkg is not None:
            setattr(services_pkg, "mcp", canon)
    except Exception:  # noqa: BLE001
        pass
    loaded: dict[str, object] = {}
    for key, mod in list(_sys.modules.items()):
        for n in _PKG_NAMES:
            pref = n + "."
            if key.startswith(pref):
                loaded.setdefault(key[len(pref):], mod)
    for suffix, mod in loaded.items():
        for n in _PKG_NAMES:
            _sys.modules[f"{n}.{suffix}"] = mod  # type: ignore[assignment]


class _SharedLoader(importlib.abc.Loader):
    """Loader that always returns/executes the same module object for both names."""

    def __init__(self, *, origin: str | None, is_package: bool,
                 submodule_search_locations=None):
        self._origin = origin
        self._is_package = is_package
        self._submodule_search_locations = submodule_search_locations
        self._mod = None

    # runpy / pkgutil inspect loaders for source; provide the inspectable surface.
    def get_filename(self, fullname):  # noqa: ANN001
        return self._origin

    def get_source(self, fullname):  # noqa: ANN001
        if not self._origin:
            return None
        with open(self._origin, "r", encoding="utf-8") as fh:
            return fh.read()

    def get_code(self, fullname):  # noqa: ANN001
        src = self.get_source(fullname)
        if src is None:
            return None
        return compile(src, self._origin or fullname, "exec", dont_inherit=True)

    def is_package(self, fullname):  # noqa: ANN001
        return self._is_package

    def create_module(self, spec):  # noqa: ANN001
        if self._mod is not None:
            return self._mod
        pair = _pair(spec.name)
        if pair is None:
            return None
        canon, alias = pair
        existing = _sys.modules.get(canon) or _sys.modules.get(alias)
        if existing is not None:
            self._mod = existing
            _sys.modules[canon] = existing
            _sys.modules[alias] = existing
            return existing
        # Create a plain module WITHOUT going through module_from_spec (which would
        # re-enter create_module via the loader and recurse).
        mod = _types.ModuleType(canon)
        mod.__file__ = self._origin
        mod.__loader__ = self
        mod.__package__ = canon.rpartition(".")[0] if "." in canon else canon
        mod.__spec__ = spec
        if self._is_package:
            locs = list(self._submodule_search_locations or [])
            mod.__path__ = locs  # type: ignore[attr-defined]
        _sys.modules[canon] = mod
        _sys.modules[alias] = mod
        self._mod = mod
        return mod

    def exec_module(self, module):  # noqa: ANN001
        if getattr(module, "_mcp_dual_exec_done", False):
            return
        origin = self._origin or getattr(module, "__file__", None)
        if not origin or origin in ("", "built-in", "frozen"):
            module._mcp_dual_exec_done = True  # type: ignore[attr-defined]
            return
        module._mcp_dual_exec_done = True  # type: ignore[attr-defined]
        try:
            with open(origin, "rb") as fh:
                source = fh.read()
            code = compile(source, origin, "exec", dont_inherit=True)
            exec(code, module.__dict__)  # noqa: S102 — loading our own package source
        except Exception:
            module._mcp_dual_exec_done = False  # type: ignore[attr-defined]
            raise
        pair = _pair(getattr(module, "__name__", "") or "")
        if pair is None and getattr(module, "__spec__", None) is not None:
            pair = _pair(module.__spec__.name)
        if pair is not None:
            canon, alias = pair
            _sys.modules[canon] = module
            _sys.modules[alias] = module


class _DualPackageFinder(importlib.abc.MetaPathFinder):
    """Force mcp_server.* and services.mcp.* onto one module object per submodule."""

    def find_spec(self, fullname, path, target=None):  # noqa: ANN001
        pair = _pair(fullname)
        if pair is None:
            return None
        canon, alias = pair

        existing = _sys.modules.get(canon) or _sys.modules.get(alias)
        if existing is not None:
            _sys.modules[canon] = existing
            _sys.modules[alias] = existing
            is_pkg = hasattr(existing, "__path__")
            return importlib.util.spec_from_loader(
                fullname,
                _SharedLoader(origin=getattr(existing, "__file__", None),
                              is_package=is_pkg),
                origin=getattr(existing, "__file__", None),
                is_package=is_pkg,
            )

        search_path = None
        if "." in canon:
            parent = _sys.modules.get(_CANON) or _sys.modules.get(_ALIAS)
            if parent is not None:
                search_path = list(getattr(parent, "__path__", []) or [])
        if not search_path and path is not None:
            search_path = list(path)

        real = importlib.machinery.PathFinder.find_spec(canon, search_path)
        if real is None and path is not None:
            real = importlib.machinery.PathFinder.find_spec(canon, list(path))
        if real is None:
            return None

        is_pkg = real.submodule_search_locations is not None
        origin = real.origin
        locs = list(real.submodule_search_locations or []) if is_pkg else None
        loader = _SharedLoader(origin=origin, is_package=is_pkg,
                               submodule_search_locations=locs)
        spec = importlib.util.spec_from_loader(
            fullname, loader, origin=origin, is_package=is_pkg,
        )
        if is_pkg and locs is not None:
            spec.submodule_search_locations = locs
        return spec


def _install_finder() -> None:
    for f in _sys.meta_path:
        if isinstance(f, _DualPackageFinder):
            return
    _sys.meta_path.insert(0, _DualPackageFinder())


_unify_dual_package()
_install_finder()
