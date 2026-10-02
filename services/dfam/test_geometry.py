"""Hermetic numerical geometry fixtures; no engine, optional ray backend or files."""
import numpy as np
import pytest

from .geometry import BuildGeometry, RayCaster, depth_peel, fit_cylinder, surface_samples, voxelize
from .mesh import PrintMesh


def box(lower=(0, 0, 0), upper=(10, 10, 10), inverted=False):
    vertices = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
                         [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], dtype=float)
    vertices = np.asarray(lower) + vertices * (np.asarray(upper) - lower)
    triangles = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
                          [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
                          [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    return PrintMesh.from_arrays(vertices, triangles[:, ::-1] if inverted else triangles)


def joined(*meshes):
    triangles, vertices, offset = [], [], 0
    for mesh in meshes:
        vertices.append(mesh.vertices)
        triangles.append(mesh.triangles + offset)
        offset += len(mesh.vertices)
    return PrintMesh.from_arrays(np.concatenate(vertices), np.concatenate(triangles))


def test_translated_rotated_build_frame_round_trip_and_winding():
    mesh = box((7, -3, 20), (11, 4, 23))
    geom = BuildGeometry(mesh, (1, 2, -3), yaw_deg=37)
    direction = np.array([1., 2., -3.]) / np.sqrt(14)
    np.testing.assert_allclose(geom.rotation @ direction, [0, 0, 1], atol=1e-14)
    np.testing.assert_allclose(geom.rotation.T @ geom.rotation, np.eye(3), atol=1e-14)
    np.testing.assert_allclose(geom.model_points(geom.vertices), mesh.vertices, atol=1e-13)
    assert geom.bounds[0, 2] == 0
    assert geom.z_min == pytest.approx((mesh.vertices @ geom.rotation.T)[:, 2].min())
    original = BuildGeometry(mesh)
    np.testing.assert_allclose(geom.normals, original.normals @ geom.rotation.T, atol=1e-14)
    np.testing.assert_allclose(geom.areas, original.areas)
    antiparallel = BuildGeometry(mesh, (0, 0, -1))
    np.testing.assert_array_equal(antiparallel.rotation, np.diag([1, -1, -1]))


def test_cube_peel_deduplicates_seams_and_retains_triangle_provenance():
    geom = BuildGeometry(box())
    peel = depth_peel(geom, cell=1)
    assert peel.shape == (10, 10)
    np.testing.assert_array_equal(peel.cells, np.repeat(np.arange(100), 2))
    np.testing.assert_allclose(peel.z, np.tile([0, 10], 100))
    np.testing.assert_array_equal(peel.sign, np.tile([-1, 1], 100))
    np.testing.assert_array_equal(peel.sign, np.sign(geom.normals[peel.tri, 2]))
    xy = peel.xy_for_cells(peel.cells)
    t = geom.triangles[peel.tri]
    assert np.all(xy >= t[:, :, :2].min(axis=1) - 1e-12)
    assert np.all(xy <= t[:, :, :2].max(axis=1) + 1e-12)
    repeated = depth_peel(geom, cell=1)
    np.testing.assert_array_equal(peel.tri, repeated.tri)
    voxels = voxelize(geom, cell=1)
    assert voxels.solid.sum() == 1000
    assert voxels.solid.shape == (12, 12, 12)
    np.testing.assert_array_equal(voxels.solid[1:-1, 1:-1, 1:-1], True)
    for axis in range(3):
        assert not np.take(voxels.solid, [0, -1], axis=axis).any()
    np.testing.assert_allclose(voxels.centers([[1, 1, 1], [10, 10, 10]]), [[.5, .5, .5], [9.5, 9.5, 9.5]])


def test_inverted_inner_cube_is_a_cavity_with_correct_normals():
    geom = BuildGeometry(joined(box(), box((3, 3, 3), (7, 7, 7), inverted=True)))
    peel = depth_peel(geom, cell=1)
    column = 4 * peel.shape[1] + 4
    selected = peel.cells == column
    np.testing.assert_allclose(peel.z[selected], [0, 3, 7, 10])
    np.testing.assert_array_equal(peel.sign[selected], [-1, 1, -1, 1])
    voxels = voxelize(geom, cell=1)
    assert voxels.solid.sum() == 1000 - 64
    assert not voxels.solid[4:8, 4:8, 4:8].any()
    caster = RayCaster(geom, 1)
    distance, triangle = caster.cast(np.array([[5., 5., 5.]]), np.array([[0., 0., 1.]]), 10,
                                    opposing_normals=np.array([[0., 0., 1.]]))
    assert distance[0] == pytest.approx(2)
    assert geom.normals[triangle[0], 2] == -1


@pytest.mark.parametrize("second", [box((5, 0, 0), (15, 10, 10)),
                                   box((0, 0, 0), (10, 10, 15)),
                                   box((0, 0, 10), (10, 10, 20))])
def test_overlapping_and_touching_boxes_are_union_not_parity(second):
    geom = BuildGeometry(joined(box(), second))
    voxels = voxelize(geom, cell=1)
    centers = voxels.centers(np.indices(voxels.solid.shape).reshape(3, -1).T)
    in_first = np.all((centers >= 0) & (centers < 10), axis=1)
    bounds = np.stack((second.vertices.min(axis=0), second.vertices.max(axis=0)))
    in_second = np.all((centers >= bounds[0]) & (centers < bounds[1]), axis=1)
    np.testing.assert_array_equal(voxels.solid.ravel(), in_first | in_second)
    peel = depth_peel(geom, cell=1)
    duplicate = (peel.cells[1:] == peel.cells[:-1]) & (np.diff(peel.z) <= 1e-6)
    assert not duplicate.any()


def test_union_surface_flags_exclude_buried_faces_and_keep_cavity_boundaries():
    geom = BuildGeometry(joined(box(), box((0, 0, 5), (10, 10, 15))))
    peel = depth_peel(geom, cell=1)
    column = peel.cells == 44
    np.testing.assert_allclose(peel.z[column], [0, 5, 10, 15])
    np.testing.assert_array_equal(peel._crossing_winding[column], [1, 1, -1, -1])
    np.testing.assert_array_equal(peel._active_surface[column], [True, False, False, True])
    shared_bottom = depth_peel(BuildGeometry(joined(box(), box((0, 0, 0), (10, 10, 15)))), 1)
    column = shared_bottom.cells == 44
    np.testing.assert_array_equal(shared_bottom._crossing_winding[column], [2, -1, -1])
    np.testing.assert_array_equal(shared_bottom._active_surface[column], [True, False, True])
    cavity = depth_peel(BuildGeometry(joined(box(), box((3, 3, 3), (7, 7, 7), True))), 1)
    np.testing.assert_array_equal(cavity._active_surface[cavity.cells == 44], True)
    contact = depth_peel(BuildGeometry(joined(box(), box((0, 0, 10), (10, 10, 20)))), 1)
    np.testing.assert_allclose(contact.z[contact.cells == 44], [0, 20])


def test_sloped_triangle_raster_returns_all_and_only_covered_centers():
    mesh = PrintMesh.from_arrays([[0, 0, 0], [4, 0, 4], [0, 4, 8]], [[0, 1, 2]])
    peel = depth_peel(BuildGeometry(mesh), 1)
    xy = peel.xy_for_cells(peel.cells)
    np.testing.assert_allclose(peel.z, xy[:, 0] + 2 * xy[:, 1])
    assert np.all(xy.sum(axis=1) <= 4)
    # Four rows of 4,3,2,1 centres, including the owned hypotenuse.
    np.testing.assert_array_equal(peel.cells, [0, 1, 2, 3, 4, 5, 6, 8, 9, 12])


def test_unwelded_triangle_soup_voxel_occupancy_matches_welded_cube():
    mesh = box()
    soup = PrintMesh.from_arrays(mesh.vertices[mesh.triangles].reshape(-1, 3), np.arange(36).reshape(-1, 3))
    np.testing.assert_array_equal(voxelize(BuildGeometry(mesh), 1).solid,
                                  voxelize(BuildGeometry(soup), 1).solid)


@pytest.mark.parametrize("requested_pitch", [.01, 1e-300])
def test_grid_limits_include_padding_and_anisotropic_shapes(requested_pitch):
    geom = BuildGeometry(box((0, 0, 0), (100, 2, 1)))
    peel = depth_peel(geom, cell=requested_pitch, max_cells=75)
    assert np.prod(peel.shape) <= 75
    assert peel.cell > .01
    voxels = voxelize(geom, cell=requested_pitch, max_cells=120)
    assert voxels.solid.size <= 120
    for axis in range(3):
        assert not np.take(voxels.solid, [0, -1], axis=axis).any()
    assert np.all(voxels.origin <= geom.bounds[0] - voxels.cell)
    assert np.all(voxels.origin + np.array(voxels.solid.shape) * voxels.cell >= geom.bounds[1] + voxels.cell - 1e-10)
    with pytest.raises(ValueError):
        voxelize(geom, cell=1, max_cells=26)


def test_thin_plate_rays_nearest_hit_limits_normal_filter_and_ties():
    geom = BuildGeometry(box((0, 0, 0), (10, 10, .02)))
    caster = RayCaster(geom, cell=.5)
    origins = np.array([[5, 5, .01], [5, 5, .01], [5, 5, 1], [11, 5, .01], [5, 5, .01]])
    directions = np.array([[0, 0, 7], [0, 0, -4], [0, 0, -1], [0, 0, 1], [0, 0, 1]])
    distance, triangle = caster.cast(origins, directions, [1, 1, 1, 1, .009])
    np.testing.assert_allclose(distance[:3], [.01, .01, .98], atol=1e-12)
    assert np.all(np.isinf(distance[3:]))
    np.testing.assert_array_equal(triangle[:3], [2, 0, 2])
    np.testing.assert_array_equal(triangle[3:], [-1, -1])
    distance, triangle = caster.cast(origins[:1], directions[:1], 1,
                                    opposing_normals=np.array([[0, 0, 1]]))
    assert np.isinf(distance[0]) and triangle[0] == -1
    distance, triangle = caster.cast(origins[:1], directions[:1], .01,
                                    opposing_normals=np.array([[0, 0, -1]]))
    assert distance[0] == pytest.approx(.01) and triangle[0] == 2


def test_rays_outside_parallel_grid_edges_and_diagonal_traversal():
    geom = BuildGeometry(box())
    caster = RayCaster(geom, 1)
    origins = np.array([[-2, -2, -2], [5, -2, 5], [10, 5, 5], [5, 5, 5], [-2, 11, 5]])
    directions = np.array([[1, 1, 1], [0, 1, 0], [-1, 0, 0], [1, 1, 1], [1, 0, 0]])
    distances, tri = caster.cast(origins, directions, np.inf)
    np.testing.assert_allclose(distances[:4], [2 * np.sqrt(3), 2, 10, 5 * np.sqrt(3)], atol=1e-12)
    assert np.isinf(distances[-1]) and tri[-1] == -1
    assert np.all(tri[:4] >= 0)
    with pytest.raises(ValueError):
        caster.cast(origins[:1], np.zeros((1, 3)), 10)


def test_ray_build_frame_and_nearest_triangle_coincident_tolerance():
    mesh = box((20, -7, 13), (30, 3, 23))
    geom = BuildGeometry(mesh, (2, 3, -5), 33)
    caster = RayCaster(geom, .7)
    model_origins = np.tile([25., -2., 18.], (5, 1))
    model_directions = np.array([[1, 2, 3], [-1, 0, 0], [1, -3, 2], [0, 1, 1], [2, 2, -1]], dtype=float)
    model_directions /= np.linalg.norm(model_directions, axis=1)[:, None]
    origins = model_origins @ geom.rotation.T - [0, 0, geom.z_min]
    directions = model_directions @ geom.rotation.T
    distance, tri = caster.cast(origins, directions, 30)
    expected = 5 / np.max(np.abs(model_directions), axis=1)
    np.testing.assert_allclose(distance, expected, atol=1e-12)
    assert np.all(tri >= 0)
    # Triangle 0 is slightly farther but the same sheet within hit tolerance.
    near_sheets = PrintMesh.from_arrays(
        [[0, 0, 1.0000005], [2, 0, 1.0000005], [0, 2, 1.0000005],
         [0, 0, 1], [2, 0, 1], [0, 2, 1]], [[0, 1, 2], [3, 4, 5]])
    sheets = BuildGeometry(near_sheets)
    distance, tri = RayCaster(sheets, 1).cast(
        np.array([[.5, .5, -1.]]), np.array([[0, 0, 1.]]), 2)
    assert tri[0] == 0
    assert distance[0] == pytest.approx(1.0000005, abs=1e-10)


def cylinder_samples(concave=False, taper=0):
    theta = np.tile(np.linspace(0, 2 * np.pi, 64, endpoint=False), 5)
    z = np.repeat(np.linspace(-8, 8, 5), 64)
    radius = 4 + taper * z
    points = np.column_stack((radius * np.cos(theta), radius * np.sin(theta), z))
    normals = np.column_stack((np.cos(theta), np.sin(theta), np.full(len(theta), -taper)))
    normals /= np.linalg.norm(normals, axis=1)[:, None]
    rotation = BuildGeometry(box(), (2, -3, 5), 23).rotation
    return points @ rotation.T + [30, -15, 7], (-normals if concave else normals) @ rotation.T, rotation[:, 2]


@pytest.mark.parametrize("concave", [False, True])
def test_cylinder_fit_recovers_axis_radius_center_and_concavity(concave):
    points, normals, axis = cylinder_samples(concave)
    fit = fit_cylinder(points, normals)
    assert fit is not None
    assert abs(np.dot(fit["axis"], axis)) == pytest.approx(1)
    np.testing.assert_allclose(fit["center"], [30, -15, 7], atol=1e-12)
    assert fit["radius"] == pytest.approx(4)
    assert fit["residual"] < 1e-12
    assert fit["concave"] == concave


def test_cylinder_fit_rejects_planes_cones_and_non_circular_sides():
    x, y = np.meshgrid(np.arange(5), np.arange(5))
    points = np.column_stack((x.ravel(), y.ravel(), np.zeros(25)))
    assert fit_cylinder(points, np.tile([0, 0, 1], (25, 1))) is None
    points, normals, _ = cylinder_samples(taper=.2)
    assert fit_cylinder(points, normals) is None
    points, normals, _ = cylinder_samples()
    points[:, 0] *= 1.6
    assert fit_cylinder(points, normals) is None


def test_surface_samples_preserve_area_and_cavity_normal_provenance():
    geom = BuildGeometry(joined(box(), box((3, 3, 3), (7, 7, 7), inverted=True)))
    points, normals, triangle, weights = surface_samples(geom, max_samples=101)
    assert weights.sum() == pytest.approx(600 + 96)
    np.testing.assert_allclose(np.bincount(triangle, weights=weights), geom.areas)
    np.testing.assert_allclose(normals, geom.normals[triangle])
    # Barycentric coordinates lie on their associated triangle, not in its box.
    t = geom.triangles[triangle]
    np.testing.assert_allclose(np.einsum("ij,ij->i", points - t[:, 0], normals), 0, atol=1e-12)
    for tri in range(len(geom.triangles)):
        np.testing.assert_allclose(points[np.flatnonzero(triangle == tri)[0]], geom.centroids[tri])
    again = surface_samples(geom, max_samples=101)
    for actual, expected in zip(again, (points, normals, triangle, weights)):
        np.testing.assert_array_equal(actual, expected)
    _, _, triangle, weights = surface_samples(geom, max_samples=7)
    assert weights.sum() == pytest.approx(696)
    assert np.all((triangle >= 0) & (triangle < len(geom.triangles)))
