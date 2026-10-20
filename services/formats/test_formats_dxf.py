"""Round-trip tests for the DXF importer (services.formats.formats_dxf via import_path).

The Python DXF lane only imports, so "round-trip" here means: author a known DXF with ezdxf, import it through
the public ``import_path`` surface, and assert the geometry (face areas, wire counts) matches.
"""
import math
import os

import pytest

ezdxf = pytest.importorskip("ezdxf")

from services.formats.formats import import_path, list_formats  # noqa: E402
from services.formats import formats_dxf  # noqa: E402


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


def _no_expansion(*args, **kwargs):
    raise AssertionError("unadmitted DXF reached virtual-entity generation")


def _nested_blocks_doc(depth):
    doc = ezdxf.new(units=4)
    blocks = [doc.blocks.new(name=f"LEVEL_{i}") for i in range(depth)]
    for i, block in enumerate(blocks[:-1]):
        block.add_blockref(blocks[i + 1].name, (1, 0))
    blocks[-1].add_line((0, 0), (1, 0))
    doc.modelspace().add_blockref(blocks[0].name, (1, 0))
    return doc


def test_nested_minsert_preserves_every_world_placement_under_nonuniform_scale(tmp_path):
    doc = ezdxf.new(units=4)
    unit = doc.blocks.new(name="UNIT")
    unit.add_line((0, 0), (1, 0))
    grid = doc.blocks.new(name="GRID")
    grid.add_blockref("UNIT", (3, 4), dxfattribs={
        "rotation": 45, "row_count": 2, "column_count": 2,
        "row_spacing": 10, "column_spacing": 20,
    })
    doc.modelspace().add_blockref("GRID", (100, 200), dxfattribs={
        "xscale": 2, "yscale": 1,
    })
    # An unreachable cyclic definition does not invalidate ordinary geometry.
    unused = doc.blocks.new(name="UNUSED")
    unused.add_blockref("UNUSED", (0, 0))
    path = tmp_path / "nested.dxf"
    doc.saveas(path)

    _, shape = import_path(path)[0]
    edges = shape.edges()
    assert len(edges) == 4
    assert [edge.length for edge in edges] == pytest.approx([math.sqrt(2.5)] * 4)
    centers = sorted((edge.center().X, edge.center().Y) for edge in edges)
    c = math.sqrt(0.5)
    expected_centers = sorted(
        (100 + 2 * (3 + (column * 20 - row * 10 + 0.5) * c),
         200 + 4 + (column * 20 + row * 10 + 0.5) * c)
        for row in range(2) for column in range(2)
    )
    for actual, expected in zip(centers, expected_centers):
        assert actual == pytest.approx(expected)


def test_nested_blocks_at_native_depth_limit_remain_importable(tmp_path):
    doc = _nested_blocks_doc(formats_dxf._MAX_BLOCK_DEPTH)
    path = tmp_path / "depth-limit.dxf"
    doc.saveas(path)
    _, shape = import_path(path)[0]
    assert len(shape.edges()) == 1
    assert shape.edges()[0].center().X == pytest.approx(formats_dxf._MAX_BLOCK_DEPTH + 0.5)


@pytest.mark.parametrize("depth", [17, 256])
def test_excessive_block_depth_refuses_before_virtual_generation(tmp_path, monkeypatch, depth):
    doc = _nested_blocks_doc(depth)
    # Importable root geometry must not turn a rejected nested block into success.
    doc.modelspace().add_line((1000, 0), (1001, 0))
    path = tmp_path / "too-deep.dxf"
    doc.saveas(path)
    monkeypatch.setattr(formats_dxf, "_placed_entities", _no_expansion)
    budget = formats_dxf._DxfExpansionBudget()
    with pytest.raises(ValueError, match="DXF expansion budget exceeded: depth"):
        formats_dxf._admit_dxf_expansion(doc.modelspace(), budget)
    assert budget.visited == formats_dxf._MAX_BLOCK_DEPTH + 1
    assert budget.instances == formats_dxf._MAX_BLOCK_DEPTH + 1
    with pytest.raises(ValueError, match="DXF expansion budget exceeded: depth"):
        import_path(path)


@pytest.mark.parametrize("rows, columns", [
    (32767, 32767), (100001, 1), (1, 100001), (0, 1), (-1, 1),
])
@pytest.mark.parametrize("spacing", [0, 1])
def test_insert_array_dimensions_refuse_before_grid_iteration(
    tmp_path, monkeypatch, rows, columns, spacing,
):
    doc = ezdxf.new(units=4)
    unit = doc.blocks.new(name="UNIT")
    unit.add_line((0, 0), (1, 0))
    msp = doc.modelspace()
    msp.add_line((1000, 0), (1001, 0))
    insert = msp.add_blockref("UNIT", (0, 0), dxfattribs={
        "row_spacing": spacing, "column_spacing": spacing,
    })
    # Match readfile's raw loading path; public setters repair nonpositive counts.
    insert.dxf.unprotected_set("row_count", rows)
    insert.dxf.unprotected_set("column_count", columns)
    path = tmp_path / "array.dxf"
    doc.saveas(path)
    monkeypatch.setattr(formats_dxf, "_placed_entities", _no_expansion)
    monkeypatch.setattr(type(insert), "multi_insert", _no_expansion)
    with pytest.raises(ValueError, match="DXF INSERT.*(dimensions|instance budget)"):
        import_path(path)


def test_reachable_block_cycle_refuses_instead_of_returning_partial_geometry(tmp_path, monkeypatch):
    doc = ezdxf.new(units=4)
    first = doc.blocks.new(name="FIRST")
    second = doc.blocks.new(name="SECOND")
    first.add_blockref("SECOND", (0, 0))
    second.add_blockref("FIRST", (0, 0))
    doc.modelspace().add_line((0, 0), (1, 0))
    doc.modelspace().add_blockref("FIRST", (0, 0))
    path = tmp_path / "cycle.dxf"
    doc.saveas(path)
    monkeypatch.setattr(formats_dxf, "_placed_entities", _no_expansion)
    with pytest.raises(ValueError, match="DXF INSERT block cycle"):
        import_path(path)


@pytest.mark.parametrize("field, ceiling", [
    ("instances", "_MAX_INSTANCES"), ("work", "_MAX_WORK"), ("edges", "_MAX_GEOMETRY"),
])
def test_cumulative_expansion_accounts_repeated_blocks_before_generation(monkeypatch, field, ceiling):
    doc = ezdxf.new(units=4)
    unit = doc.blocks.new(name="UNIT")
    unit.add_line((0, 0), (1, 0))
    grid = doc.blocks.new(name="GRID")
    grid.add_blockref("UNIT", (0, 0), dxfattribs={
        "row_count": 2, "column_count": 2, "row_spacing": 5, "column_spacing": 5,
    })
    msp = doc.modelspace()
    msp.add_blockref("GRID", (0, 0))
    msp.add_blockref("GRID", (100, 0))
    budget = formats_dxf._DxfExpansionBudget()
    formats_dxf._admit_dxf_expansion(msp, budget)
    assert budget.visited == 12  # two roots, two child INSERTs, eight placed LINEs
    assert budget.instances == 10
    assert budget.edges == 8
    repeated = formats_dxf._DxfExpansionBudget()
    formats_dxf._admit_dxf_expansion(msp, repeated)
    assert vars(repeated) == vars(budget)

    needed = getattr(budget, field)
    monkeypatch.setattr(formats_dxf, ceiling, needed)
    _, shape = formats_dxf.bodies_from_doc(doc, "admitted")[0]
    assert len(shape.edges()) == 8
    monkeypatch.setattr(formats_dxf, ceiling, needed - 1)
    monkeypatch.setattr(formats_dxf, "_placed_entities", _no_expansion)
    with pytest.raises(ValueError, match=f"DXF expansion budget exceeded: {field}"):
        formats_dxf.bodies_from_doc(doc, "refused")
