"""Tests for the DWG importer (engine.doc.formats_dwg via import_path).

DWG has no free in-process reader, so import goes DWG→DXF (external converter) → the DXF pipeline.
These tests exercise the wiring deterministically WITHOUT requiring a converter to be installed:
the registry entry, the graceful no-converter error, and the convert→bodies glue (via a
monkeypatched converter). A real end-to-end round-trip runs only when a converter is present.
"""
import math

import pytest

ezdxf = pytest.importorskip("ezdxf")

from engine.doc.formats import import_path, list_formats  # noqa: E402
from engine.doc import formats_dwg  # noqa: E402


def _square_circle_doc():
    """An in-memory ezdxf Drawing standing in for a converted DWG: square (area 100) + circle r2."""
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
    msp.add_circle((5, 5), 2.0)
    return doc


def test_dwg_in_format_registry():
    fmts = {f["fmt"]: f for f in list_formats()}
    assert "dwg" in fmts
    info = fmts["dwg"]
    assert ".dwg" in info["exts"]
    assert info["can_import"] is True
    assert info["can_export"] is False


def test_dwg_no_converter_raises_actionable(monkeypatch, tmp_path):
    # Force the "no converter installed" state regardless of the host machine.
    monkeypatch.setattr(formats_dwg, "_select_converter", lambda: None)
    p = tmp_path / "x.dwg"
    p.write_bytes(b"not a real dwg")  # never read — converter selection fails first
    with pytest.raises(ValueError, match="DWG→DXF converter"):
        import_path(str(p))


def test_dwg_conversion_wiring(monkeypatch, tmp_path):
    # Stub the converter to return a known drawing; assert the DXF pipeline turns it into faces.
    monkeypatch.setattr(formats_dwg, "_select_converter",
                        lambda: (lambda _path: _square_circle_doc()))
    p = tmp_path / "part.dwg"
    p.write_bytes(b"stub")
    bodies = import_path(str(p))
    assert len(bodies) == 1
    name, shape = bodies[0]
    assert name == "part"
    areas = sorted(round(f.area, 3) for f in shape.faces())
    assert areas[0] == pytest.approx(math.pi * 4.0, abs=0.05)  # circle r=2
    assert areas[1] == pytest.approx(100.0, abs=0.01)          # square 10x10


def test_dwg_converter_failure_is_clean(monkeypatch, tmp_path):
    def _boom(_path):
        raise RuntimeError("converter crashed")
    monkeypatch.setattr(formats_dwg, "_select_converter", lambda: _boom)
    p = tmp_path / "bad.dwg"
    p.write_bytes(b"stub")
    with pytest.raises(ValueError, match="conversion .* failed"):
        import_path(str(p))


@pytest.mark.skipif(not formats_dwg.converter_available(),
                    reason="no DWG→DXF converter (ODA File Converter / dwg2dxf) on this machine")
def test_dwg_real_roundtrip(tmp_path):
    # Build a DXF, export it to DWG via the ODA converter, then import the DWG back.
    from ezdxf.addons import odafc
    doc = _square_circle_doc()
    dwg = tmp_path / "rt.dwg"
    try:
        odafc.export_dwg(doc, str(dwg), version="AC1027")
    except Exception as exc:  # noqa: BLE001 — only ODA FC can write DWG; skip if that path fails
        pytest.skip(f"could not author a .dwg fixture via odafc: {exc}")
    _, shape = import_path(str(dwg))[0]
    areas = sorted(round(f.area, 2) for f in shape.faces())
    assert areas[-1] == pytest.approx(100.0, abs=0.5)
