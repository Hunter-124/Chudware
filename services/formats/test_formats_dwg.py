"""Tests for the DWG importer (services.formats.formats_dwg via import_path).

DWG has no free in-process reader, so import goes DWG→DXF (external converter) → the DXF pipeline.
These tests exercise the wiring deterministically WITHOUT requiring a converter to be installed:
the registry entry, the graceful no-converter error, and the convert→bodies glue (via a
monkeypatched converter). A real end-to-end round-trip runs only when a converter is present.
"""
import math

import pytest

ezdxf = pytest.importorskip("ezdxf")

from services.formats.formats import import_path, list_formats  # noqa: E402
from services.formats import formats_dwg  # noqa: E402


def _square_circle_doc():
    """An in-memory ezdxf Drawing standing in for a converted DWG: square (area 100) + circle r2."""
    doc = ezdxf.new(units=4)  # fixture coordinates describe millimetres
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
    assert info["can_export"] is True  # native export_file writes DWG with its own encoder


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


def test_dwg2dxf_timeout_is_clean(monkeypatch, tmp_path):
    observed = {}

    def _hang(_argv, **kwargs):
        observed["timeout"] = kwargs.get("timeout")
        raise formats_dwg.subprocess.TimeoutExpired(_argv, kwargs["timeout"])

    monkeypatch.setattr(formats_dwg, "_select_converter",
                        lambda: formats_dwg._read_dwg_via_dwg2dxf)
    monkeypatch.setattr(formats_dwg, "_dwg2dxf_path", lambda: "/fake/dwg2dxf")
    monkeypatch.setattr(formats_dwg.subprocess, "run", _hang)
    p = tmp_path / "hung.dwg"
    p.write_bytes(b"stub")

    with pytest.raises(ValueError, match=r"dwg2dxf timed out after 120s"):
        import_path(str(p))
    assert observed["timeout"] == 120


def test_preferred_odafc_timeout_is_clean_and_restores_runner(monkeypatch, tmp_path):
    from ezdxf.addons import odafc

    observed = {}
    original_runner = odafc._run_with_no_gui

    def _hang(_argv, **kwargs):
        observed["timeout"] = kwargs.get("timeout")
        raise formats_dwg.subprocess.TimeoutExpired(_argv, kwargs["timeout"])

    def _fake_readfile(_path):
        return odafc._run_with_no_gui("Darwin", "/fake/ODAFileConverter", ["arg"])

    monkeypatch.setattr(formats_dwg, "_select_converter",
                        lambda: formats_dwg._read_dwg_via_odafc)
    monkeypatch.setattr(odafc, "readfile", _fake_readfile)
    monkeypatch.setattr(formats_dwg.subprocess, "run", _hang)
    p = tmp_path / "hung-oda.dwg"
    p.write_bytes(b"stub")

    with pytest.raises(ValueError, match=r"ODA File Converter timed out after 120s"):
        import_path(str(p))
    assert observed["timeout"] == formats_dwg._CONVERTER_TIMEOUT_S
    assert odafc._run_with_no_gui is original_runner


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
