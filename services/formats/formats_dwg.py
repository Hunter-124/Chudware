"""DWG (AutoCAD binary drawing) import — external DWG→DXF converter + the DXF importer.

DWG is a proprietary binary format with **no free in-process reader** (native DWG fidelity is the
Open Design Alliance's whole business). Rather than parse it ourselves, we mirror the
``formats_solidworks`` strategy: convert DWG→DXF with an external converter **the user provides**
(never bundled, never linked — subprocess / mere aggregation only), then reuse the DXF importer's
entity→geometry pipeline (:func:`services.formats.formats_dxf.bodies_from_doc`). Every entry point
degrades cleanly with an actionable error when no converter is present.

Converters, in priority order (subprocess only, PATH/registry-discovered, never bundled):
  1. **ODA File Converter** (free, cross-platform) — wrapped by ``ezdxf.addons.odafc``.
  2. **LibreDWG ``dwg2dxf``** — GPL-3.0, so **subprocess-only** per the CONTRIBUTING license
     guardrails (never linked into libaicad, never redistributed). Found on ``$PATH``.

License posture: identical to the pandoc / LibreOffice converters already in
``contracts/licenses.md`` — optional, PATH-discovered, subprocess-only, never bundled; absence is
not an error, it degrades to a clear "install a converter" message.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

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
    """Path to LibreDWG's ``dwg2dxf`` on PATH, or None."""
    return shutil.which("dwg2dxf")


def converter_available() -> bool:
    """True iff at least one DWG→DXF converter is usable on this machine."""
    return _odafc_available() or _dwg2dxf_path() is not None


def _read_dwg_via_odafc(path: Path):
    """DWG → ezdxf ``Drawing`` via the ODA File Converter (ezdxf.addons.odafc)."""
    from ezdxf.addons import odafc
    return odafc.readfile(str(path))


def _read_dwg_via_dwg2dxf(path: Path):
    """DWG → ezdxf ``Drawing`` by shelling out to LibreDWG ``dwg2dxf`` into a temp .dxf."""
    import ezdxf

    exe = _dwg2dxf_path()
    if not exe:
        raise RuntimeError("dwg2dxf not on PATH")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / (path.stem + ".dxf")
        # dwg2dxf writes DXF to -o <out>; keep it a plain subprocess (mere aggregation, GPL-safe).
        proc = subprocess.run([exe, "-o", str(out), str(path)],
                              capture_output=True, text=True)
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
    "DWG import needs an external DWG→DXF converter (none found). Install ONE of:\n"
    "  • ODA File Converter (free): https://www.opendesign.com/guestfiles/oda_file_converter "
    "(ezdxf.addons.odafc finds it automatically), or\n"
    "  • LibreDWG's `dwg2dxf` on your PATH.\n"
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
