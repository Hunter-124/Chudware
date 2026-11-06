"""The same similarity transform places every reconstruction artifact."""
import numpy as np
import pytest

from services.vision.recon_transform import ReconTransform


def _transform():
    return ReconTransform(2.0, [[0, -1, 0], [1, 0, 0], [0, 0, 1]],
                          [7, -3, 4], source="two_point", residual_mm=0.2,
                          confirmed=True)


def test_transform_json_roundtrip_and_apply():
    transform = _transform()
    decoded = ReconTransform.from_json(transform.to_json())
    points = np.array([[10, 2, 1], [-1, 4, 3]], dtype=float)
    np.testing.assert_allclose(decoded.apply(points), [[3, 17, 6], [-1, -5, 10]])
    assert decoded.to_json() == transform.to_json()
    assert ReconTransform.identity().confirmed is False
    np.testing.assert_allclose(ReconTransform.identity().apply(points), points)


def test_reject_invalid_similarity():
    for scale in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            ReconTransform(scale, np.eye(3).tolist(), [0, 0, 0], "test")
    with pytest.raises(ValueError):
        ReconTransform(1, [[2, 0, 0], [0, 1, 0], [0, 0, 1]], [0, 0, 0], "test")
    with pytest.raises(ValueError):
        ReconTransform(1, [[-1, 0, 0], [0, 1, 0], [0, 0, 1]], [0, 0, 0], "test")


def test_mesh_and_point_cloud_use_same_origin_and_transform(tmp_path):
    o3d = pytest.importorskip("open3d")
    from services.vision.mesh_cleanup import (apply_transform_to_mesh,
                                              apply_transform_to_ply_points,
                                              cleanup_mesh)

    vertices = np.array([[10., 2, 1], [11, 2, 1], [10, 3, 1]])
    mesh = o3d.geometry.TriangleMesh()
    mesh.vertices = o3d.utility.Vector3dVector(vertices)
    mesh.triangles = o3d.utility.Vector3iVector([[0, 1, 2]])
    mesh.compute_vertex_normals()
    source = tmp_path / "source.ply"
    assert o3d.io.write_triangle_mesh(str(source), mesh)

    # The old AABB-center pivot left this off-origin triangle near x=10.
    scaled = tmp_path / "scaled.ply"
    result = cleanup_mesh(str(source), str(scaled), mm_per_unit=2.0)
    assert result["ok"] and result["scale_applied"]
    np.testing.assert_allclose(np.asarray(o3d.io.read_triangle_mesh(str(scaled)).vertices),
                               vertices * 2)

    transformed = tmp_path / "transformed.ply"
    transform = _transform()
    assert apply_transform_to_mesh(source, transform, transformed)["ok"]
    np.testing.assert_allclose(np.asarray(o3d.io.read_triangle_mesh(str(transformed)).vertices),
                               transform.apply(vertices))
    np.testing.assert_allclose(
        np.asarray(o3d.io.read_triangle_mesh(str(transformed)).vertex_normals),
        np.tile([0., 0., 1.], (3, 1)))
    transformed_cleanup = tmp_path / "cleaned.ply"
    assert cleanup_mesh(str(source), str(transformed_cleanup),
                        recon_transform=transform)["ok"]
    np.testing.assert_allclose(np.asarray(o3d.io.read_triangle_mesh(str(transformed_cleanup)).vertices),
                               transform.apply(vertices))

    cloud = o3d.geometry.PointCloud()
    cloud.points = o3d.utility.Vector3dVector(vertices)
    cloud.colors = o3d.utility.Vector3dVector([[1., 0, 0], [0, 1, 0], [0, 0, 1]])
    cloud.normals = o3d.utility.Vector3dVector([[1., 0, 0]] * 3)
    cloud_src, cloud_dst = tmp_path / "points.ply", tmp_path / "points_mm.ply"
    assert o3d.io.write_point_cloud(str(cloud_src), cloud)
    assert apply_transform_to_ply_points(cloud_src, transform.to_json(), cloud_dst)["ok"]
    output = o3d.io.read_point_cloud(str(cloud_dst))
    np.testing.assert_allclose(np.asarray(output.points), transform.apply(vertices))
    np.testing.assert_allclose(np.asarray(output.colors), np.asarray(cloud.colors), atol=1/255)
    np.testing.assert_allclose(np.asarray(output.normals), np.tile([0., 1., 0.], (3, 1)))
