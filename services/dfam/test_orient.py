"""Behavioral coverage for deterministic approximate orientation ranking."""
import math

import numpy as np
import pytest
import trimesh

from services.dfam.geometry import BuildGeometry
from services.dfam.mesh import FaceInfo, PrintMesh
from services.dfam.orient import best_bed_yaw, evaluate_orientation, optimize_orientation
from services.dfam.params import PrintParams


def box(size):
    mesh = trimesh.creation.box(extents=size)
    return PrintMesh.from_arrays(mesh.vertices, mesh.faces)


def bracket():
    # A watertight L cross section in XZ, extruded 10 mm along Y.
    xz = np.array([[0., 0.], [30., 0.], [30., 4.], [4., 4.], [4., 30.], [0., 30.]])
    vertices = np.array([[x, y, z] for y in (0., 10.) for x, z in xz])
    caps = [[0, 1, 2], [0, 2, 3], [0, 3, 5], [3, 4, 5]]
    triangles = [cap for cap in caps] + [[i + 6 for i in cap[::-1]] for cap in caps]
    for i in range(6):
        j = (i + 1) % 6
        triangles.extend([[i, i + 6, j + 6], [i, j + 6, j]])
    mesh = trimesh.Trimesh(vertices=vertices, faces=triangles, process=False)
    mesh.fix_normals()
    return PrintMesh.from_arrays(mesh.vertices, mesh.faces)


def attributed_box(size):
    mesh = trimesh.creation.box(extents=size)
    normals = mesh.face_normals
    directions = np.r_[np.eye(3), -np.eye(3)]
    tri_face = np.argmax(normals @ directions.T, axis=1)
    faces = [FaceInfo(f'face-{i}', 1, 0, 'extrude') for i in range(6)]
    return PrintMesh(mesh.vertices, mesh.faces, tri_face, faces)


def test_l_bracket_prefers_stable_flat_zero_support():
    rows = optimize_orientation(bracket(), PrintParams(), goal='min_support')
    best = rows[0]
    assert best['metrics']['support_volume_mm3'] == 0
    assert abs(best['build_dir'][1]) == pytest.approx(1)
    assert best['metrics']['contact_area_mm2'] == pytest.approx(224)
    assert best['metrics']['height_mm'] == pytest.approx(10)
    assert best['metrics']['tip_ratio'] < 1


def test_strength_direction_prefers_layer_plane():
    mesh = box((60., 20., 4.))
    ordinary = optimize_orientation(mesh, PrintParams(), top_k=1)[0]
    strengthened = optimize_orientation(mesh, PrintParams(), goal='strength',
                                        strength_dir=(0, 0, 1), top_k=1)[0]
    assert abs(ordinary['build_dir'][2]) == pytest.approx(1)
    assert strengthened['metrics']['strength_out_of_plane'] == pytest.approx(0)
    assert abs(strengthened['build_dir'][2]) < 1e-8


def test_preserved_sid_avoids_bed_or_support():
    mesh = attributed_box((40., 30., 4.))
    ordinary = optimize_orientation(mesh, PrintParams(), top_k=1)[0]
    assert ordinary['build_dir'][2] == pytest.approx(-1)
    rows = optimize_orientation(mesh, PrintParams(), preserve_faces=('face-2',), top_k=6)
    assert rows[0]['build_dir'][2] == pytest.approx(1)
    assert rows[0]['metrics']['preserved_faces_affected'] == 0
    inverted = next(row for row in rows if row['build_dir'][2] == -1)
    assert inverted['metrics']['preserved_faces_on_bed'] == 1
    assert inverted['metrics']['preserved_faces_affected'] == 1
    assert inverted['metrics']['preserved_face_sids'] == ['face-2']
    assert inverted['score'] > rows[0]['score']


def test_thin_bar_fits_diagonal_even_when_min_area_yaw_does_not():
    mesh = box((280., 5., 4.))
    geom = BuildGeometry(mesh)
    params = PrintParams(bed_size_mm=(220., 220., 250.))
    unturned = evaluate_orientation(mesh, params, (0, 0, 1))
    assert unturned['metrics']['fits_bed'] is False
    yaw, footprint, fits = best_bed_yaw(geom, params)
    assert fits
    assert 0 < yaw < 90
    assert max(footprint) <= 220 + 1e-7
    evaluated = evaluate_orientation(mesh, params, (0, 0, 1), yaw)
    assert evaluated['metrics']['fits_bed'] is True
    ranked = optimize_orientation(mesh, params, top_k=1)[0]
    assert ranked['metrics']['fits_bed'] is True
    assert ranked['metrics']['height_mm'] == pytest.approx(4)
    assert max(ranked['metrics']['footprint_mm']) <= 220


def test_continuous_yaw_fit_on_rectangular_bed():
    mesh = box((280., 3., 4.))
    params = PrintParams(bed_size_mm=(250., 150., 250.))
    assert evaluate_orientation(mesh, params, (0, 0, 1), 45)['metrics']['fits_bed'] is False
    yaw, footprint, fits = best_bed_yaw(BuildGeometry(mesh), params)
    assert fits
    assert footprint[0] <= 250 + 1e-7
    assert footprint[1] <= 150 + 1e-7
    assert evaluate_orientation(mesh, params, (0, 0, 1), yaw)['metrics']['fits_bed'] is True


def test_reported_rounded_yaw_still_fits():
    mesh = box((280., 5., 4.))
    params = PrintParams(bed_size_mm=(220.02, 220.02, 250.))
    row = optimize_orientation(mesh, params, top_k=1)[0]
    assert row['metrics']['fits_bed'] is True
    actual = evaluate_orientation(mesh, params, row['build_dir'], row['yaw_deg'])
    assert actual['metrics']['fits_bed'] is True
    assert actual['metrics']['footprint_mm'] == row['metrics']['footprint_mm']


def test_evaluate_does_not_mutate_supplied_direction():
    direction = np.array([0., 0., 2.])
    evaluate_orientation(box((10., 10., 10.)), PrintParams(), direction)
    assert np.array_equal(direction, [0., 0., 2.])


def test_optimizer_is_deterministic_and_rotation_maps_build_direction():
    mesh = bracket()
    params = PrintParams(bed_size_mm=(220, 220, 250))
    first = optimize_orientation(mesh, params, strength_dir=(1, 2, 3))
    assert first == optimize_orientation(mesh, params, strength_dir=(1, 2, 3))
    for row in first:
        assert np.asarray(row['rotation']) @ row['build_dir'] == pytest.approx([0, 0, 1], abs=1e-7)
        assert row['rank'] >= 1


def test_nearest_support_landing_not_always_bed():
    # A lower platform leaves a 2 mm air gap below a separate upper plate.
    lower = trimesh.creation.box(extents=(20, 20, 10))
    lower.apply_translation((0, 0, 5))
    upper = trimesh.creation.box(extents=(20, 20, 2))
    upper.apply_translation((0, 0, 13))
    joined = trimesh.util.concatenate((lower, upper))
    mesh = PrintMesh.from_arrays(joined.vertices, joined.faces)
    metrics = evaluate_orientation(mesh, PrintParams(), (0, 0, 1))['metrics']
    assert metrics['support_volume_mm3'] == pytest.approx(800)
    assert metrics['overhang_area_mm2'] == pytest.approx(400)


def test_preserved_support_face_count_is_unique_per_sid():
    lower = trimesh.creation.box(extents=(20, 20, 10))
    lower.apply_translation((0, 0, 5))
    upper = trimesh.creation.box(extents=(20, 20, 2))
    upper.apply_translation((0, 0, 13))
    joined = trimesh.util.concatenate((lower, upper))
    tags = np.zeros(len(joined.faces), dtype=np.int32)
    tags[len(lower.faces):] = (upper.face_normals[:, 2] < -.99).astype(np.int32)
    mesh = PrintMesh(joined.vertices, joined.faces, tags,
                     [FaceInfo(None, 1, 6, None), FaceInfo('ceiling', 2, 0, 'plate')])
    rows = optimize_orientation(mesh, PrintParams(), preserve_faces=('ceiling',), top_k=6)
    upright = next(row for row in rows if row['build_dir'][2] == 1)
    assert upright['metrics']['preserved_faces_on_support'] == 1
    assert upright['metrics']['preserved_faces_on_bed'] == 0
    assert upright['metrics']['preserved_faces_affected'] == 1
    assert rows[0]['metrics']['preserved_faces_affected'] == 0


def test_overlapping_solids_do_not_add_internal_support():
    lower = trimesh.creation.box(extents=(20, 20, 10))
    lower.apply_translation((0, 0, 5))
    upper = trimesh.creation.box(extents=(20, 20, 10))
    upper.apply_translation((0, 0, 10))
    joined = trimesh.util.concatenate((lower, upper))
    mesh = PrintMesh.from_arrays(joined.vertices, joined.faces)
    metrics = evaluate_orientation(mesh, PrintParams(), (0, 0, 1))['metrics']
    assert metrics['support_volume_mm3'] == 0
    assert metrics['overhang_area_mm2'] == 0


def test_shared_bottom_multiplicity_uses_union_top_for_landing():
    lower = trimesh.creation.box(extents=(20, 20, 10))
    lower.apply_translation((0, 0, 5))
    taller = trimesh.creation.box(extents=(20, 20, 15))
    taller.apply_translation((0, 0, 7.5))
    upper = trimesh.creation.box(extents=(20, 20, 2))
    upper.apply_translation((0, 0, 18))
    joined = trimesh.util.concatenate((lower, taller, upper))
    mesh = PrintMesh.from_arrays(joined.vertices, joined.faces)
    metrics = evaluate_orientation(mesh, PrintParams(), (0, 0, 1))['metrics']
    assert metrics['support_volume_mm3'] == pytest.approx(800)
    assert metrics['overhang_area_mm2'] == pytest.approx(400)


@pytest.mark.parametrize('direction', [(0, 0, 0), (math.nan, 0, 1)])
def test_invalid_strength_direction_rejected(direction):
    with pytest.raises(ValueError, match='strength_dir'):
        optimize_orientation(box((10, 10, 10)), PrintParams(), strength_dir=direction)
