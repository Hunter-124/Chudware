"""services/browser/driver.py — browser backend factory.

Selects both the residual Playwright-compatible driver used by headed login /
embed / cookie-export surfaces and the headless Parts Library primitive.

Env
---
``CHUDWARE_PW_BACKEND``:

- ``cef`` — CEF daemon for headless fetch/download; stock Playwright for
  residual surfaces.
- ``playwright`` — stock Playwright for residual and headless paths.
- ``patchright`` — patched Playwright driver for residual and headless paths
  (``pip install patchright``).
- ``camoufox`` — optional Firefox anti-detect fallback (import-guarded) for
  residual experiments; headless fetch/download remain on CEF.

When the env is unset, Darwin defaults to ``playwright`` because macOS ships
CEF off. Linux and Windows continue to default to ``cef``. Playwright is an
intentional platform fallback, not a CEF replacement: challenge detection
remains detect-never-solve and bot-walled pages fail cleanly.

Never auto-downloads browser binaries at import time. Call
:func:`provision_browser` (descriptor-driven, explicit ``install=True``) to
fetch Chromium / Camoufox builds.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any, Callable

#: Env key that selects the residual PW-compatible backend.
ENV_PW_BACKEND = "CHUDWARE_PW_BACKEND"

#: Canonical backend ids. Unknown values fall back to the platform default.
BACKENDS: tuple[str, ...] = ("cef", "playwright", "patchright", "camoufox")

#: Non-Darwin default. Darwin uses ``playwright`` because its host ships CEF off.
DEFAULT_BACKEND = "cef"
DARWIN_DEFAULT_BACKEND = "playwright"

# ---------------------------------------------------------------------------
# Provisioning descriptors (reveng-style; offline-safe until install=True)
# ---------------------------------------------------------------------------
# mode:
#   "cli"  -> run an explicit install command (never at import).
#   "env"  -> discovery-only; user supplies the binary (we never fetch).
# kind: "package" (pip) | "browser" (binary payload of a package).
SHA_PENDING = "PENDING"

BROWSER_DESCRIPTORS: dict[str, dict[str, Any]] = {
    "playwright": {
        "kind": "package",
        "license": "Apache-2.0",
        "mode": "cli",
        "pip": "playwright",
        "browser_cmd": [sys.executable, "-m", "playwright", "install", "chromium"],
        "sha256": SHA_PENDING,
        "note": (
            "Stock Playwright Python package + Chromium. "
            "`pip install playwright` then provision_browser('playwright', install=True)."
        ),
    },
    "patchright": {
        "kind": "package",
        "license": "Apache-2.0",
        "mode": "cli",
        "pip": "patchright",
        "browser_cmd": [sys.executable, "-m", "patchright", "install", "chromium"],
        "sha256": SHA_PENDING,
        "note": (
            "Patched Playwright driver (Runtime.enable avoidance, automation-flag "
            "scrub). `pip install patchright` then "
            "provision_browser('patchright', install=True)."
        ),
    },
    "camoufox": {
        "kind": "package",
        "license": "MIT",
        "mode": "cli",
        "pip": "camoufox[geoip]",
        "browser_cmd": [sys.executable, "-m", "camoufox", "fetch"],
        "sha256": SHA_PENDING,
        "note": (
            "Firefox C++ anti-detect browser + PW-like launcher. Experimental "
            "2026 builds. `pip install camoufox[geoip]` then "
            "provision_browser('camoufox', install=True). Opt-in hard-site "
            "fallback only — not the residual login/embed default."
        ),
    },
    "cef": {
        "kind": "binary",
        "license": "BSD-style (Chromium/CEF)",
        "mode": "env",
        "env_var": None,  # host ships Chudware --cef-daemon; no Python fetch
        "url": None,
        "sha256": None,
        "note": (
            "Primary headless path (host CEF daemon). Not provisioned by this "
            "module — the GUI/host binary owns the CEF build."
        ),
    },
}


def default_backend(platform: str | None = None) -> str:
    """Return the backend default for ``platform`` (or this interpreter)."""
    return DARWIN_DEFAULT_BACKEND if (platform or sys.platform) == "darwin" else DEFAULT_BACKEND


def selected_backend(platform: str | None = None) -> str:
    """Return the normalised env selection, or the platform-specific default."""
    raw = (os.environ.get(ENV_PW_BACKEND) or "").strip().lower()
    if raw in BACKENDS:
        return raw
    return default_backend(platform)


def headless_backend(platform: str | None = None) -> str:
    """Backend for Parts Library fetch/download.

    Only stock Playwright and Patchright opt the headless seam away from CEF.
    Camoufox remains an import-guarded residual experiment.
    """
    backend = selected_backend(platform)
    return backend if backend in ("playwright", "patchright") else "cef"


def headless_client(*, profile: str = "", platform: str | None = None) -> Any:
    """Return a client exposing CEF-compatible ``fetch``/``download`` methods."""
    if headless_backend(platform) in ("playwright", "patchright"):
        from . import pw_client
        return pw_client.client(profile)
    from . import cef_client
    return cef_client.client()


def _not_installed_msg(name: str) -> str:
    d = BROWSER_DESCRIPTORS.get(name, {})
    pip = d.get("pip") or name
    return (
        f"{name} not installed — run `pip install {pip}` and "
        f"provision_browser({name!r}, install=True) "
        f"(see services/browser/README.md / driver.BROWSER_DESCRIPTORS)"
    )


def _import_playwright_sync() -> Callable[[], Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(_not_installed_msg("playwright")) from exc
    return sync_playwright


def _import_playwright_async() -> Callable[[], Any]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(_not_installed_msg("playwright")) from exc
    return async_playwright


def _import_patchright_sync() -> Callable[[], Any]:
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(_not_installed_msg("patchright")) from exc
    return sync_playwright


def _import_patchright_async() -> Callable[[], Any]:
    try:
        from patchright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(_not_installed_msg("patchright")) from exc
    return async_playwright


def _import_camoufox_sync() -> Callable[[], Any]:
    """Import-guarded Camoufox entry.

    Camoufox's public surface is a Playwright-*compatible* launcher
    (``Camoufox`` context manager), not a free drop-in for
    ``sync_playwright()``. Residual session/embed paths expect the
    Playwright factory shape — raise a clear RuntimeError so callers
    degrade rather than call a mismatched API. Availability is still
    probeable via :func:`backend_available`.
    """
    try:
        import camoufox  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(_not_installed_msg("camoufox")) from exc
    # Package present but not a sync_playwright drop-in for residual paths.
    raise RuntimeError(
        "camoufox is an opt-in Firefox hard-site backend, not a drop-in "
        "sync_playwright() for residual login/embed — set "
        "CHUDWARE_PW_BACKEND=patchright (or leave unset for cef/stock "
        "playwright) for those surfaces; use a camoufox-specific fetch path "
        "for hard sites after provision_browser('camoufox', install=True)"
    )


def _import_camoufox_async() -> Callable[[], Any]:
    try:
        import camoufox  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(_not_installed_msg("camoufox")) from exc
    raise RuntimeError(
        "camoufox is an opt-in Firefox hard-site backend, not a drop-in "
        "async_playwright() for residual embed — set CHUDWARE_PW_BACKEND="
        "patchright (or leave unset for cef/stock playwright); provision "
        "camoufox only for hard-site fallbacks"
    )


def resolve_sync() -> Callable[[], Any]:
    """Return the sync Playwright-compatible factory for residual PW paths.

    - ``cef`` (default) → stock ``playwright.sync_api.sync_playwright``
    - ``patchright`` → ``patchright.sync_api.sync_playwright``
    - ``camoufox`` → import-guarded; raises RuntimeError (not a drop-in)

    Raises ``RuntimeError`` (never bare ImportError) so session/embed keep
    their ``playwright_not_installed`` degradation shape.
    """
    backend = selected_backend()
    if backend == "patchright":
        return _import_patchright_sync()
    if backend == "camoufox":
        return _import_camoufox_sync()
    # cef default — stock playwright residual path (byte-identical).
    return _import_playwright_sync()


def resolve_async() -> Callable[[], Any]:
    """Async twin of :func:`resolve_sync` for embed.py."""
    backend = selected_backend()
    if backend == "patchright":
        return _import_patchright_async()
    if backend == "camoufox":
        return _import_camoufox_async()
    return _import_playwright_async()


def backend_available(name: str | None = None) -> bool:
    """True when the Python package for *name* (or the selected backend) imports.

    ``cef`` is always ``True`` from this module's POV — the host binary owns
    the daemon; we do not probe it here. Unknown names → ``False``.
    """
    backend = (name or selected_backend()).strip().lower()
    if backend == "cef":
        return True
    if backend == "patchright":
        try:
            import patchright  # noqa: F401
            return True
        except ImportError:
            return False
    if backend == "camoufox":
        try:
            import camoufox  # noqa: F401
            return True
        except ImportError:
            return False
    if backend == "playwright":
        try:
            import playwright  # noqa: F401
            return True
        except ImportError:
            return False
    return False


def provision_status(name: str | None = None) -> dict[str, Any]:
    """Offline readiness for a browser backend descriptor (no network).

    Returns a small status dict: ``{ok, name, available, mode, note, hint?}``.
    Never installs and never touches the network.
    """
    key = (name or selected_backend()).strip().lower()
    desc = BROWSER_DESCRIPTORS.get(key)
    if desc is None:
        return {
            "ok": False,
            "name": key,
            "available": False,
            "mode": None,
            "note": None,
            "hint": f"unknown backend {key!r}; known: {', '.join(BROWSER_DESCRIPTORS)}",
        }
    avail = backend_available(key)
    hint = None
    if not avail and desc.get("mode") == "cli":
        pip = desc.get("pip") or key
        hint = (
            f"pip install {pip} && "
            f"python -c \"from services.browser.driver import provision_browser; "
            f"provision_browser({key!r}, install=True)\""
        )
    return {
        "ok": avail if desc.get("mode") != "env" else True,
        "name": key,
        "available": avail,
        "mode": desc.get("mode"),
        "license": desc.get("license"),
        "note": desc.get("note"),
        "hint": hint,
    }


def provision_browser(name: str, *, install: bool = False) -> dict[str, Any]:
    """Provision (or report) a browser backend by descriptor name.

    Default ``install=False`` is a pure offline status probe — safe at any
    time, no network. With ``install=True`` runs the descriptor's explicit
    browser-fetch CLI (``playwright/patchright install chromium`` or
    ``python -m camoufox fetch``). **Never** called from import paths.

    ``cef`` is host-owned: status only, install is a no-op with a hint.
    """
    key = (name or "").strip().lower()
    desc = BROWSER_DESCRIPTORS.get(key)
    if desc is None:
        return {
            "ok": False,
            "name": key,
            "reason": "unknown_backend",
            "hint": f"known: {', '.join(sorted(BROWSER_DESCRIPTORS))}",
        }

    status = provision_status(key)
    if not install:
        return {**status, "installed": False, "action": "status_only"}

    if desc.get("mode") == "env":
        return {
            **status,
            "ok": True,
            "installed": False,
            "action": "skipped_env_mode",
            "hint": desc.get("note") or "discovery-only backend; nothing to fetch",
        }

    # Ensure the Python package is importable before running its browser CLI.
    if not backend_available(key if key != "playwright" else "playwright"):
        pip = desc.get("pip") or key
        return {
            **status,
            "ok": False,
            "installed": False,
            "action": "package_missing",
            "reason": "package_not_installed",
            "hint": f"pip install {pip} first, then re-run with install=True",
        }

    cmd = list(desc.get("browser_cmd") or [])
    if not cmd:
        return {
            **status,
            "ok": False,
            "installed": False,
            "action": "no_browser_cmd",
            "reason": "no_browser_cmd",
        }

    # Refuse to invent network fetches for PENDING-hash download descriptors
    # that aren't CLI-driven (none today; guard for future url-mode entries).
    if desc.get("mode") == "download" and desc.get("sha256") == SHA_PENDING:
        return {
            **status,
            "ok": False,
            "installed": False,
            "action": "refused_pending_sha",
            "reason": "sha_pending",
            "hint": "descriptor sha256 is PENDING — refuse silent fetch",
        }

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            **status,
            "ok": False,
            "installed": False,
            "action": "browser_cmd_failed",
            "reason": "browser_cmd_failed",
            "hint": str(exc),
            "cmd": cmd,
        }

    ok = proc.returncode == 0
    return {
        **status,
        "ok": ok,
        "installed": ok,
        "action": "browser_cmd",
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout_tail": (proc.stdout or "")[-500:],
        "stderr_tail": (proc.stderr or "")[-500:],
        "hint": None if ok else (
            f"command failed (rc={proc.returncode}): {' '.join(cmd)}"
        ),
    }


def which_browser_cmd(name: str) -> list[str] | None:
    """Return the provision CLI argv for *name*, or None if unknown/env-mode."""
    desc = BROWSER_DESCRIPTORS.get((name or "").strip().lower())
    if not desc or desc.get("mode") != "cli":
        return None
    cmd = desc.get("browser_cmd")
    return list(cmd) if cmd else None


def describe_backends() -> dict[str, Any]:
    """Summary for health / diagnostics — offline, no imports of optional deps
    beyond :func:`backend_available` probes."""
    return {
        "selected": selected_backend(),
        "default": default_backend(),
        "headless": headless_backend(),
        "env_key": ENV_PW_BACKEND,
        "env_value": os.environ.get(ENV_PW_BACKEND),
        "backends": {k: provision_status(k) for k in BROWSER_DESCRIPTORS},
        "path_python": sys.executable,
        "which_patchright": shutil.which("patchright"),
        "which_playwright": shutil.which("playwright"),
    }
