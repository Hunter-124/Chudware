"""DWG (AutoCAD binary drawing) import — external DWG→DXF converter + the DXF importer.

DWG is a proprietary binary format with **no free in-process reader** (native DWG fidelity is the
Open Design Alliance's whole business). Rather than parse it ourselves, we mirror the
``formats_solidworks`` strategy: convert DWG→DXF with an external converter (never bundled, never
linked — subprocess / mere aggregation only), then reuse the DXF importer's entity→geometry
pipeline (:func:`services.formats.formats_dxf.bodies_from_doc`). Every entry point degrades
cleanly with an actionable error when no converter is present.

Converters, in priority order (subprocess only, never bundled):
  1. **ODA File Converter** (free, cross-platform) — wrapped by ``ezdxf.addons.odafc``; preferred
     when installed.
  2. **LibreDWG ``dwg2dxf``** — GPL-3.0, so **subprocess-only** per the CONTRIBUTING license
     guardrails (never linked into libchudware, never redistributed). Resolved by
     :func:`services.formats.libredwg.tool`: ``$PATH`` first, then the per-user managed install
     that ``python3 scripts/provision_libredwg.py`` builds from the pinned upstream release.

DWG *writing* is not done here: the native engine ``export_file`` (format ``dwg``) writes
R2000 DWG with Chudware's own encoder (``core/doc/dwg_write.cpp``), without LibreDWG.

License posture: identical to the pandoc / LibreOffice converters already in
``contracts/licenses.md`` — optional, subprocess-only, never bundled; absence is not an error, it
degrades to a clear "install or provision a converter" message.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional

_CONVERTER_TIMEOUT_S = 120
_ODA_RUN_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Converter discovery — each returns an ezdxf ``Drawing`` for a .dwg, or None if unavailable.
# ---------------------------------------------------------------------------


def _odafc_available() -> bool:
    try:
        from ezdxf.addons import odafc
        return bool(odafc.is_installed())
    except Exception:  # noqa: BLE001 — ezdxf/odafc import or probe failure ⇒ treat as unavailable
        return False


def _dwg2dxf_path() -> Optional[str]:
    """Path to LibreDWG's ``dwg2dxf`` (PATH, then the provisioned managed install), or None."""
    from services.formats.libredwg import tool
    return tool("dwg2dxf")


def converter_available() -> bool:
    """True iff at least one DWG→DXF converter is usable on this machine."""
    return _odafc_available() or _dwg2dxf_path() is not None


def _run_odafc_bounded(system: str, command: str, arguments: list[str]):
    """ezdxf ODA runner with the same hard deadline as the LibreDWG path."""
    from ezdxf.addons import odafc

    argv = [command, *arguments]
    if system == "Linux":
        with odafc._linux_dummy_display() as display:
            env = os.environ.copy()
            env["DISPLAY"] = display
            return subprocess.run(
                argv,
                text=True,
                capture_output=True,
                env=env,
                timeout=_CONVERTER_TIMEOUT_S,
            )
    if system == "Darwin":
        return subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=_CONVERTER_TIMEOUT_S,
        )
    if system == "Windows":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags = (
            subprocess.CREATE_NEW_CONSOLE | subprocess.STARTF_USESHOWWINDOW
        )
        startupinfo.wShowWindow = subprocess.SW_HIDE
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=startupinfo,
        )
        try:
            proc.wait(timeout=_CONVERTER_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
        return proc
    raise odafc.UnsupportedPlatform(f"Unsupported platform: {system}")


def _read_dwg_via_odafc(path: Path):
    """DWG → ezdxf ``Drawing`` via a deadline-bounded ODA File Converter."""
    from ezdxf.addons import odafc

    # ezdxf does not expose a timeout, but funnels every platform launch through
    # this one runner. Serialize the temporary override so concurrent imports
    # cannot restore each other's function or escape the deadline.
    with _ODA_RUN_LOCK:
        original_runner = odafc._run_with_no_gui
        odafc._run_with_no_gui = _run_odafc_bounded
        try:
            return odafc.readfile(str(path))
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"ODA File Converter timed out after {_CONVERTER_TIMEOUT_S}s"
            ) from exc
        finally:
            odafc._run_with_no_gui = original_runner


def _read_dwg_via_dwg2dxf(path: Path):
    """DWG → ezdxf ``Drawing`` by shelling out to LibreDWG ``dwg2dxf`` into a temp .dxf."""
    import ezdxf

    exe = _dwg2dxf_path()
    if not exe:
        raise RuntimeError(
            "dwg2dxf not found (run `python3 scripts/provision_libredwg.py`)")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / (path.stem + ".dxf")
        # dwg2dxf writes DXF to -o <out>; keep it a plain subprocess (mere aggregation, GPL-safe).
        try:
            proc = subprocess.run(
                [exe, "-o", str(out), str(path)],
                capture_output=True,
                text=True,
                timeout=_CONVERTER_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"dwg2dxf timed out after {_CONVERTER_TIMEOUT_S}s"
            ) from exc
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(
                f"dwg2dxf failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
        return ezdxf.readfile(str(out))


def _select_converter() -> Optional[Callable[[Path], Any]]:
    """Return a ``dwg_path -> ezdxf.Drawing`` converter, preferring ODA FC, else dwg2dxf, else None."""
    if _odafc_available():
        return _read_dwg_via_odafc
    if _dwg2dxf_path() is not None:
        return _read_dwg_via_dwg2dxf
    return None


# ---------------------------------------------------------------------------
# Public import entry point (called from services.formats.formats.import_path)
# ---------------------------------------------------------------------------

_NO_CONVERTER_HINT = (
    "DWG import needs an external DWG→DXF converter (none found). Provide ONE of:\n"
    "  • LibreDWG `dwg2dxf`: run `python3 scripts/provision_libredwg.py` to build the pinned "
    "release into Chudware's per-user tools folder, or put `dwg2dxf` on your PATH, or\n"
    "  • ODA File Converter (free, preferred when installed): "
    "https://www.opendesign.com/guestfiles/oda_file_converter "
    "(ezdxf.addons.odafc finds it automatically).\n"
    "Alternatively, open the file in a CAD app and Save As DXF, then import the .dxf directly."
)


def import_dwg(path: str | Path) -> list[tuple[str, Any]]:
    """Import a DWG file → ``[(name, shape)]`` by converting to DXF then running the DXF pipeline.

    Raises ``ValueError`` with an actionable hint when no DWG→DXF converter is available, or when
    the converted drawing carries no importable 2D geometry.
    """
    from services.formats.formats_dxf import bodies_from_doc

    path = Path(path)
    convert = _select_converter()
    if convert is None:
        raise ValueError(_NO_CONVERTER_HINT)

    try:
        doc = convert(path)
    except Exception as exc:  # noqa: BLE001 — surface converter failures as a clean import error
        raise ValueError(f"DWG→DXF conversion of '{path}' failed: {exc}") from exc

    return bodies_from_doc(doc, path.stem or "dwg", source=str(path))
