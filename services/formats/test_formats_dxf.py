"""Round-trip tests for the DXF importer (services.formats.formats_dxf via import_path).

The Python DXF lane only imports, so "round-trip" here means: author a known DXF with ezdxf, import it through
the public ``import_path`` surface, and assert the geometry (face areas, wire counts) matches.
"""
import math
import os

import pytest

ezdxf = pytest.importorskip("ezdxf")

from services.formats.formats import import_path, list_formats  # noqa: E402


def _write(tmp_path, build) -> str:
    doc = ezdxf.new(units=4)  # fixture coordinates describe millimetres
    build(doc.modelspace())
    p = os.path.join(str(tmp_path), "case.dxf")
    doc.saveas(p)
    return p


def test_dxf_in_format_registry():
    fmts = {f["fmt"]: f for f in list_formats()}
    assert "dxf" in fmts, "DXF must appear in the format matrix (host derives filters from it)"
    info = fmts["dxf"]
    assert ".dxf" in info["exts"]
    assert info["can_import"] is True
    assert info["can_export"] is True  # written by the native engine's export_file


def test_dxf_square_and_circle_become_faces(tmp_path):
    def build(msp):
        msp.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)  # area 100
        msp.add_circle((5, 5), 2.0)                                            # area ~12.566
    p = _write(tmp_path, build)

    bodies = import_path(p)
    assert len(bodies) == 1
    name, shape = bodies[0]
    assert name == "case"
    areas = sorted(round(f.area, 3) for f in shape.faces())
    assert areas[0] == pytest.approx(math.pi * 4.0, abs=0.05)   # circle r=2
    assert areas[1] == pytest.approx(100.0, abs=0.01)           # square 10x10

@pytest.mark.parametrize("insunits, expected", [
    (0, 2.0),     # unitless geometry remains untouched
    (1, 50.8),    # inches -> mm
    (6, 2000.0),  # metres -> mm
])
def test_dxf_insunits_scales_geometry_to_mm(tmp_path, insunits, expected):
    def build(msp):
        msp.doc.header["$INSUNITS"] = insunits
        msp.add_line((3, 0), (5, 0))

    _, shape = import_path(_write(tmp_path, build))[0]
    assert len(shape.edges()) == 1
    assert shape.edges()[0].length == pytest.approx(expected)
    assert shape.edges()[0].center().X == pytest.approx(expected * 2)


def test_dxf_open_geometry_stays_as_wire(tmp_path):
    def build(msp):
        msp.add_line((20, 0), (25, 5))     # lone open segment
        msp.add_arc((0, 0), 3.0, 0, 90)    # lone open arc
    p = _write(tmp_path, build)

    _, shape = import_path(p)[0]
    assert len(shape.faces()) == 0          # nothing closed → no faces
    assert len(shape.edges()) >= 2          # the line + the arc survive as edges/wires


def test_dxf_bulge_polyline_arc(tmp_path):
    # D-shape (half-disc): a 180deg CCW bulge cap over a straight diameter. Verifies the bulge arc
    # both in radius and in direction (bulge>0 = CCW → cap bows to the left of the segment).
    def build(msp):
        # (0,0)->(10,0) with bulge=1 is a semicircle bowing +y (apex ~ (5,5)); closed by the
        # straight return (10,0)->(0,0). Area = half a disc of radius 5.
        msp.add_lwpolyline([(0, 0, 1.0), (10, 0, 0.0)], format="xyb", close=True)
    p = _write(tmp_path, build)

    _, shape = import_path(p)[0]
    faces = shape.faces()
    assert len(faces) == 1
    assert faces[0].area == pytest.approx(math.pi * 5.0 ** 2 / 2.0, abs=0.05)  # half-disc r=5
    # apex bows to +y (CCW), so the centroid sits above the diameter
    assert faces[0].center().Y > 1.0


def test_dxf_insert_block_is_exploded(tmp_path):
    def build(msp):
        doc = msp.doc
        blk = doc.blocks.new(name="UNIT")
        blk.add_lwpolyline([(0, 0), (4, 0), (4, 4), (0, 4)], close=True)  # 4x4 square, area 16
        msp.add_blockref("UNIT", (100, 100))                              # placed far away
    p = _write(tmp_path, build)

    _, shape = import_path(p)[0]
    faces = shape.faces()
    assert len(faces) == 1
    assert faces[0].area == pytest.approx(16.0, abs=0.01)
    # exploded geometry is world-placed at the insert point, not left at the origin
    cx, cy, _ = faces[0].center().to_tuple()
    assert cx == pytest.approx(102.0, abs=0.5)
    assert cy == pytest.approx(102.0, abs=0.5)


def test_dxf_ellipse_flattens_to_curve(tmp_path):
    def build(msp):
        # full ellipse, semi-axes 5 and 2.5 → closed curve, area ~= pi*a*b = 39.27
        msp.add_ellipse((0, 0), major_axis=(5, 0), ratio=0.5, start_param=0, end_param=math.tau)
    p = _write(tmp_path, build)

    _, shape = import_path(p)[0]
    # flattened ellipse is a closed spline → one face (approximate area within a few %)
    faces = shape.faces()
    assert len(faces) == 1
    assert faces[0].area == pytest.approx(math.pi * 5.0 * 2.5, rel=0.03)


def test_dxf_empty_document_raises(tmp_path):
    p = _write(tmp_path, lambda msp: None)
    with pytest.raises(ValueError, match="no importable 2D geometry"):
        import_path(p)


def test_dxf_detected_by_extension(tmp_path):
    # import_path with no explicit format must route .dxf to the DXF importer.
    p = _write(tmp_path, lambda msp: msp.add_circle((0, 0), 1.0))
    bodies = import_path(p, format=None)
    assert len(bodies) == 1 and len(bodies[0][1].faces()) == 1
