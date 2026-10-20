"""slice_part's slicer-input STL keeps the slicing mesh's topology, or the slice is refused.

STL stores float32. The 6-axis wrist's canonical export held two real vertices one float32
step (1.9e-6 mm) apart at model y=23.8969; slice_part rested the posed part on z=0, which
moved them to z=48.8969, where the float32 step is 3.8e-6 mm, so the written STL merged
them into two zero-area triangles. PrusaSlicer deleted those facets on load
(``degenerate_facets="2" facets_removed="2"`` in its 3MF) and slice_part still reported
print_ready. The fixture below rebuilds that exact pair on a closed box with the wrist's
bounding box. No engine or slicer needed.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import numpy as np
import pytest

from services.mcp import print_tools as pt

# The wrist's pair (model frame, float32) and bounding box.
_PAIR_Y = np.float32(23.896934509277344)
_PAIR_Z = np.float32(268.1001281738281)
_BOX = (-23.0, 11.5, -25.0, 25.1, 239.1, 290.1)
_WRIST_BUILD_DIR = [0.0, 1.0, 0.0]


def write_sliver_box(path: Path) -> Path:
    """A closed 34.5 x 50.1 x 51 mm box whose +X face is fanned around two vertices one
    float32 step apart in y (two needle triangles share that 1.9e-6 mm edge)."""
    import trimesh

    f = np.float32
    x0, x1, y0, y1, z0, z1 = (f(v) for v in _BOX)
    pair_b = np.nextafter(_PAIR_Y, f(100))
    vertices = np.array([
        [x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
        [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1],
        [x1, _PAIR_Y, _PAIR_Z], [x1, pair_b, _PAIR_Z]], dtype=np.float32)
    faces = np.array([
        [0, 3, 2], [0, 2, 1], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
        [3, 7, 6], [3, 6, 2], [0, 4, 7], [0, 7, 3],
        # +X face: P0=1, P1=2, P2=6, P3=5 fanned around A=8, B=9
        [1, 2, 9], [2, 6, 9], [6, 5, 8], [5, 1, 8], [1, 9, 8], [9, 6, 8]])
    mesh = trimesh.Trimesh(vertices.astype(np.float64), faces, process=False)
    assert mesh.is_watertight and mesh.is_winding_consistent
    mesh.export(path)
    return path


def _raw_stl_topology(path: Path) -> tuple[int, int]:
    """(exact distinct vertices, triangles with a repeated corner) of a binary STL as written."""
    data = path.read_bytes()
    count = int(np.frombuffer(data[80:84], dtype="<u4")[0])
    record = np.frombuffer(data[84:84 + 50 * count],
                           dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    _, ids = np.unique(record["v"].reshape(-1, 3), axis=0, return_inverse=True)
    corners = ids.reshape(-1, 3)
    repeated = ((corners[:, 0] == corners[:, 1]) | (corners[:, 1] == corners[:, 2])
                | (corners[:, 0] == corners[:, 2]))
    return int(ids.max()) + 1, int(repeated.sum())


@pytest.fixture
def wrist_pose(tmp_path):
    source = {"kind": "file", "path": str(write_sliver_box(tmp_path / "sliver.stl"))}
    orient = {"build_dir": _WRIST_BUILD_DIR, "yaw_deg": 0.0}
    return pt._oriented_arrays(source, orient, tmp_path, "model_frame.stl")


def test_the_old_bed_resting_pose_merges_the_pair(wrist_pose):
    """Guards the fixture: the pose slice_part used to write does merge this pair."""
    model, posed, triangles = wrist_pose
    assert len(model) == 10
    resting = posed - np.array([0.0, 0.0, posed[:, 2].min()])
    evidence = pt._float32_collapse(model, resting, triangles)
    assert evidence is not None
    assert evidence["merged_vertex_groups"] == 1 and evidence["collapsed_triangles"] == 2


def test_the_origin_centred_pose_is_written_exactly(wrist_pose, tmp_path):
    model, posed, triangles = wrist_pose
    low, high = posed.min(axis=0), posed.max(axis=0)
    assert np.allclose((low + high) / 2, 0.0, atol=1e-4)
    assert pt._float32_collapse(model, posed, triangles) is None
    out = tmp_path / "wrist.oriented.stl"
    pt._write_slicer_stl(out, "wrist", "origin-centred", model, posed, triangles)
    assert _raw_stl_topology(out) == (10, 0)


@pytest.mark.parametrize("build_dir", [[1, 0, 0], [-1, 0, 0], [0, -1, 0], [0, 0, 1],
                                       [0, 0, -1]])
def test_every_axis_pose_keeps_the_pair(tmp_path, build_dir):
    source = {"kind": "file", "path": str(write_sliver_box(tmp_path / "sliver.stl"))}
    model, posed, triangles = pt._oriented_arrays(
        source, {"build_dir": build_dir, "yaw_deg": 0.0}, tmp_path, "model_frame.stl")
    assert pt._float32_collapse(model, posed, triangles) is None


def test_the_raw_bbox_centre_shift_would_merge_the_pair_on_a_rounding_tie(wrist_pose):
    """Why the shift is snapped: the unsnapped centre lands both vertices on one tie."""
    model, posed, triangles = wrist_pose
    from services.dfam import frame_for_build_dir

    rotated = model @ np.asarray(frame_for_build_dir(_WRIST_BUILD_DIR, 0.0)).T
    low, high = rotated.min(axis=0), rotated.max(axis=0)
    assert pt._float32_collapse(model, rotated - (low + high) / 2, triangles) is not None
    snapped = pt._float32_exact_shift(rotated, (low + high) / 2)
    assert np.allclose(rotated - snapped, posed)


def test_a_plate_position_that_merges_the_pair_is_refused_with_model_coordinates(
        wrist_pose, tmp_path):
    model, posed, triangles = wrist_pose
    placed = posed - pt._float32_exact_shift(posed, [-117.5, -117.5, posed[:, 2].min()])
    out = tmp_path / "01_wrist_1.stl"
    with pytest.raises(pt.SlicerMeshRefusal) as caught:
        pt._write_slicer_stl(out, "wrist", "plate", model, placed, triangles)
    assert not out.exists()
    refusal = caught.value.to_dict()
    assert refusal["ok"] is False and refusal["reason"] == "slicer_mesh_precision_loss"
    assert refusal["merged_vertex_groups"] == 1 and refusal["collapsed_triangles"] == 2
    group = refusal["groups"][0]
    assert sorted(round(p[1], 7) for p in group["model_mm"]) == [
        round(float(_PAIR_Y), 7), round(float(np.nextafter(_PAIR_Y, np.float32(100))), 7)]
    assert group["model_separation_mm"] < 2e-6 < max(group["float32_step_mm"])
    assert "slice wrist on its own" in refusal["content"]


def test_a_triangle_flattened_without_merging_vertices_is_refused(tmp_path):
    """Three distinct vertices can still round onto one line: no merged group, zero area."""
    base = np.float32(20.0)
    apex = np.nextafter(base, np.float32(100))  # one float32 step (2**-19 mm) above
    model = np.array([[0.0, 0.0, base], [30.0, 0.0, base], [15.0, 0.0, apex]],
                     dtype=np.float32).astype(np.float64)
    triangles = np.array([[0, 1, 2]])
    assert pt._float32_collapse(model, model, triangles) is None
    lifted = model + np.array([0.0, 0.0, 25.0])  # step 2**-18 at 45 mm: the apex ties onto z=45
    assert len(np.unique(lifted.astype(np.float32), axis=0)) == 3
    out = tmp_path / "needle.stl"
    with pytest.raises(pt.SlicerMeshRefusal) as caught:
        pt._write_slicer_stl(out, "needle", "plate", model, lifted, triangles)
    assert not out.exists()
    refusal = caught.value.to_dict()
    assert refusal["merged_vertex_groups"] == 0 and refusal["groups"] == []
    assert refusal["collapsed_triangles"] == 1
    assert refusal["collapsed"][0]["model_area_mm2"] == pytest.approx(15 * 2.0 ** -19)
    assert "exactly zero area" in refusal["content"]


def test_exact_zero_area_is_decided_exactly_not_by_tolerance():
    needle = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.5, 1e-30, 0.0]])
    line = np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 1.0], [0.25, 0.25, 0.25]])
    tri = np.array([[0, 1, 2]])
    assert not pt._zero_area(needle, tri)[0]
    assert pt._zero_area(line, tri)[0]
    assert pt._zero_area(np.array([[1.0, 2, 3], [1.0, 2, 3], [4.0, 5, 6]]), tri)[0]


def _project_3mf(path: Path, volumes: list[str], *, config: bool = True) -> Path:
    body = "".join(f'<volume firstid="0" lastid="1">{v}</volume>' for v in volumes)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("3D/3dmodel.model", "<model/>")
        if config:
            archive.writestr("Metadata/Slic3r_PE_model.config",
                             f'<?xml version="1.0"?><config><object id="1">{body}</object>'
                             f'</config>')
    path.write_bytes(buffer.getvalue())
    return path


_CLEAN = ('<mesh edges_fixed="0" degenerate_facets="0" facets_removed="0" '
          'facets_reversed="0" backwards_edges="0"/>')
_REPAIRED = ('<mesh edges_fixed="0" degenerate_facets="2" facets_removed="2" '
             'facets_reversed="0" backwards_edges="0"/>')


def test_slicer_mesh_repairs_sum_every_volume(tmp_path):
    clean = pt._slicer_mesh_repairs(_project_3mf(tmp_path / "a.3mf", [_CLEAN]), volumes=1)
    assert clean["available"] and not any(clean["counts"].values())
    plate = pt._slicer_mesh_repairs(_project_3mf(tmp_path / "b.3mf", [_CLEAN, _REPAIRED]),
                                    volumes=2)
    assert plate["available"] and plate["volumes"] == 2
    assert plate["counts"]["degenerate_facets"] == 2 and plate["counts"]["facets_removed"] == 2


@pytest.mark.parametrize("case", ["no_path", "missing_file", "not_zip", "no_config",
                                  "bad_xml", "volume_count", "no_mesh", "missing_key",
                                  "not_integer"])
def test_a_missing_or_malformed_repair_record_is_unavailable_not_clean(tmp_path, case):
    target = tmp_path / f"{case}.3mf"
    if case == "no_path":
        target = None
    elif case == "not_zip":
        target.write_text("not a zip")
    elif case == "no_config":
        _project_3mf(target, [_CLEAN], config=False)
    elif case == "bad_xml":
        _project_3mf(target, ["<mesh"])
    elif case == "volume_count":
        _project_3mf(target, [_CLEAN])
    elif case == "no_mesh":
        _project_3mf(target, ['<metadata type="volume" key="name" value="x"/>'])
    elif case == "missing_key":
        _project_3mf(target, [_CLEAN.replace(' backwards_edges="0"', "")])
    elif case == "not_integer":
        _project_3mf(target, [_CLEAN.replace('edges_fixed="0"', 'edges_fixed="-1"')])
    record = pt._slicer_mesh_repairs(target, volumes=2 if case == "volume_count" else 1)
    assert record["available"] is False and record["reason"], record
    assert "counts" not in record
