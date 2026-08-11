"""Crash-bundle writer and consent-gated diagnostics dispatch.

On an unhandled exception or fatal signal, we write a local crash bundle
(JSON sidecar) into <appdata>/Chudware/crashes/ and – ONLY at consent
tier "full" – invoke an upload dispatch hook so diagnostics can be sent to
the team.  Default consent is "off" (no data leaves the machine).

Usage (called once at server startup):
    from services.mcp import crash_bundle
    crash_bundle.install()

The upload hook is intentionally a no-op stub; wire in a real HTTP call only
when a genuine collection endpoint exists.  Tests assert the stub is called
at "full" and NOT called at "off"/"meta".
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import signal
import sys
import traceback
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Crash directory (mirrors appdata_root() convention)
# ---------------------------------------------------------------------------

def _crash_dir() -> Path:
    """Return (and create) the directory where crash bundles are written.

    Override with CHUDWARE_CRASH_DIR for tests.  Falls through to the same
    XDG-aware appdata root that autosave uses so everything stays together.
    """
    override = os.environ.get("CHUDWARE_CRASH_DIR")
    if override:
        d = Path(override)
    else:
        try:
            from services import paths as _paths
            d = _paths.ROOT / "crashes"  # alias-aware root, not the always-new dir
        except Exception:
            d = Path(os.environ.get("APPDATA") or "/tmp") / "Chudware" / "crashes"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Upload dispatch hook (stub — wire in a real endpoint when one exists)
# ---------------------------------------------------------------------------

# Replaceable upload function for tests and future real implementations.
# Signature: (bundle_path: Path, bundle: dict) -> None
# Must be idempotent and best-effort — never allowed to raise.
_upload_hook: Optional[Callable[[Path, dict], None]] = None


def set_upload_hook(fn: Callable[[Path, dict], None]) -> None:
    """Replace the upload hook (used by tests and future production wiring)."""
    global _upload_hook
    _upload_hook = fn


def _default_upload_stub(bundle_path: Path, bundle: dict) -> None:  # noqa: ARG001
    """No-op upload stub.  Replace with a real HTTP call when a collection
    endpoint exists.  Logs to show the code path is reached in tests."""
    log.debug("crash_bundle: upload stub called for %s (no endpoint configured)", bundle_path)


# ---------------------------------------------------------------------------
# Consent resolution
# ---------------------------------------------------------------------------

def _current_consent_tier() -> str:
    """Read the active consent tier from agent config (best-effort, never raises)."""
    try:
        from services.agent.config import consent_tier, load_config
        return consent_tier(load_config())
    except Exception:
        return "off"


# ---------------------------------------------------------------------------
# Bundle writer
# ---------------------------------------------------------------------------

_MAX_LOG_TAIL_LINES = 100
_MAX_BUNDLES = 20  # oldest pruned when this is exceeded


def _log_tail(n: int = _MAX_LOG_TAIL_LINES) -> list[str]:
    """Return the last *n* lines from the stderr log (best-effort)."""
    try:
        log_path = _crash_dir().parent / "services.mcp.log"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return lines[-n:]
    except Exception:
        pass
    return []


def _autosave_refs() -> list[str]:
    """List filenames of crash-time autosaves (step+ stems) for the bundle."""
    try:
        # Import lazily — cad_tools may not be importable in all test configs.
        # Relative so it shares module identity with the rest of the package.
        from .cad_tools import _autosave_dir as cad_autosave_dir
        return [p.stem for p in cad_autosave_dir().glob("*.step+")]
    except Exception:
        return []


def _prune_old_bundles(crash_dir: Path, max_bundles: int = _MAX_BUNDLES) -> None:
    bundles = sorted(crash_dir.glob("crash_*.json"), key=lambda p: p.stat().st_mtime)
    while len(bundles) > max_bundles:
        try:
            bundles.pop(0).unlink(missing_ok=True)
        except OSError:
            break


def write_crash_bundle(
    exc_type: type | None = None,
    exc_value: BaseException | None = None,
    tb: object | None = None,
    *,
    source: str = "exception",
) -> Optional[Path]:
    """Write a crash bundle to disk.  Returns the Path on success, None on failure.

    Always writes locally.  Dispatches to the upload hook ONLY when consent is "full".
    """
    try:
        crash_dir = _crash_dir()
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bundle_path = crash_dir / f"crash_{ts}.json"

        stack_lines: list[str] = []
        if exc_type is not None:
            stack_lines = traceback.format_exception(exc_type, exc_value, tb)
        elif tb is not None:
            # Called from a signal handler with a current-frame traceback.
            stack_lines = traceback.format_stack()

        bundle: dict = {
            "schema": "crash_bundle_v1",
            "ts_utc": ts,
            "source": source,
            "python": sys.version,
            "argv": sys.argv[:],
            "stack": "".join(stack_lines),
            "log_tail": _log_tail(),
            "autosave_refs": _autosave_refs(),
        }

        # Write atomically.
        tmp = bundle_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
        os.replace(tmp, bundle_path)
        log.warning("crash_bundle: wrote %s", bundle_path)

        _prune_old_bundles(crash_dir)

        # Upload ONLY at full consent.
        tier = _current_consent_tier()
        if tier == "full":
            hook = _upload_hook or _default_upload_stub
            try:
                hook(bundle_path, bundle)
            except Exception:  # noqa: BLE001
                pass

        return bundle_path
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Signal handler & top-level exception hook
# ---------------------------------------------------------------------------

_INSTALLED = False


def install() -> None:
    """Install crash-handler hooks (idempotent).  Call once at server startup."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    # Unhandled-exception hook — fires before the interpreter prints and exits.
    _prev_excepthook = sys.excepthook

    def _excepthook(exc_type: type, exc_value: BaseException, tb: object) -> None:
        write_crash_bundle(exc_type, exc_value, tb, source="uncaught_exception")
        _prev_excepthook(exc_type, exc_value, tb)

    sys.excepthook = _excepthook

    # SIGTERM handler — e.g. systemd / Docker stop; not SIGKILL (unkillable), but useful.
    try:
        _prev_sigterm = signal.getsignal(signal.SIGTERM)

        def _sigterm_handler(signum: int, frame: object) -> None:  # noqa: ARG001
            write_crash_bundle(source="SIGTERM")
            if callable(_prev_sigterm):
                _prev_sigterm(signum, frame)

        signal.signal(signal.SIGTERM, _sigterm_handler)
    except Exception:  # noqa: BLE001
        pass

    # SIGABRT — triggered by some C++ abort() paths.
    try:
        _prev_sigabrt = signal.getsignal(signal.SIGABRT)

        def _sigabrt_handler(signum: int, frame: object) -> None:  # noqa: ARG001
            write_crash_bundle(source="SIGABRT")
            if callable(_prev_sigabrt):
                _prev_sigabrt(signum, frame)

        signal.signal(signal.SIGABRT, _sigabrt_handler)
    except Exception:  # noqa: BLE001
        pass


def reset_for_testing() -> None:
    """Undo install() — restores default excepthook/signals.  Tests only."""
    global _INSTALLED
    if not _INSTALLED:
        return
    sys.excepthook = sys.__excepthook__
    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    except Exception:  # noqa: BLE001
        pass
    try:
        signal.signal(signal.SIGABRT, signal.SIG_DFL)
    except Exception:  # noqa: BLE001
        pass
    _INSTALLED = False


def show_crash_dir() -> str:
    """Return the local crash folder path (for a 'show folder' UX action)."""
    return str(_crash_dir())
