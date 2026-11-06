"""Consumer contracts: Gaussian crops preserve payloads and refuse corrupt data."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from services.vision.gaussian_pipeline import crop_splat, inspect_splat


# Intentionally reordered, with custom integer and SH properties: a crop must
# not assume packed xyz first or rebuild a reduced property list.
_PROPERTIES = [
    ("uchar", "label"), ("float", "opacity"), ("float", "z"),
    ("float", "scale_0"), ("float", "rot_0"), ("float", "x"),
    ("float", "f_dc_0"), ("float", "rot_1"), ("float", "scale_1"),
    ("float", "f_dc_1"), ("float", "y"), ("float", "rot_2"),
    ("float", "scale_2"), ("float", "rot_3"), ("float", "f_dc_2"),
    ("double", "custom_weight"),
]


def _ply(encoding="binary_little_endian"):
    header = ("ply\nformat " + encoding + " 1.0\ncomment keep this metadata\n"
              "element vertex 3\n" + "".join(f"property {kind} {name}\n" for kind, name in _PROPERTIES)
              + "end_header\n").encode("ascii")
    records = []
    for i, x in enumerate((-2.0, 0.0, 2.0)):
        values = {name: 0.25 for _, name in _PROPERTIES}
        values.update(label=i + 5, x=x, y=1.0, z=-1.0, rot_0=1.0, custom_weight=1.23456789012345)
        ordered = [values[name] for _, name in _PROPERTIES]
        if encoding == "ascii":
            records.append(("  ".join(str(value) for value in ordered) + "\n").encode("ascii"))
        else:
            endian = "<" if encoding == "binary_little_endian" else ">"
            records.append(struct.pack(endian + "B" + "f" * 14 + "d", *ordered))
    return header, records


@pytest.mark.parametrize("encoding", ["ascii", "binary_little_endian", "binary_big_endian"])
def test_crop_keeps_all_selected_property_bytes_and_source(tmp_path, encoding):
    header, records = _ply(encoding)
    original = header + b"".join(records)
    source, target = tmp_path / "source.ply", tmp_path / "crop.ply"
    source.write_bytes(original)
    result = crop_splat(source, target, [-0.5, 0, -2, 0.5, 2, 0])
    assert result["ok"]
    assert result["bounds"] == [0, 1, -1, 0, 1, -1]
    assert result["count"] == 1
    assert target.read_bytes() == header.replace(b"element vertex 3", b"element vertex 1") + records[1]
    assert source.read_bytes() == original
    assert inspect_splat(target)["bounds"] == result["bounds"]


def test_truncated_crop_cannot_replace_previous_preview(tmp_path):
    header, records = _ply()
    source, target = tmp_path / "source.ply", tmp_path / "preview.ply"
    valid = header + b"".join(records)
    source.write_bytes(valid[:-1])
    target.write_bytes(valid)
    result = crop_splat(source, target, [-3, -3, -3, 3, 3, 3])
    assert not result["ok"]
    assert target.read_bytes() == valid
    with pytest.raises(ValueError):
        inspect_splat(source)


def test_empty_crop_preserves_preview_and_can_recrop_original(tmp_path):
    header, records = _ply()
    source, target = tmp_path / "source.ply", tmp_path / "preview.ply"
    valid = header + b"".join(records)
    source.write_bytes(valid)
    target.write_bytes(valid)
    rejected = crop_splat(source, target, [10, 10, 10, 20, 20, 20])
    assert rejected["reason"] == "empty_crop"
    assert target.read_bytes() == valid
    accepted = crop_splat(source, target, [-3, 0, -2, 3, 2, 0])
    assert accepted["ok"] and accepted["count"] == 3
    assert target.read_bytes() == valid


def test_nonfinite_nonposition_property_is_not_a_valid_artifact(tmp_path):
    header, records = _ply("ascii")
    tokens = records[0].split()
    tokens[1] = b"nan"  # opacity, not xyz
    records[0] = b" ".join(tokens) + b"\n"
    path = tmp_path / "bad.ply"
    path.write_bytes(header + b"".join(records))
    with pytest.raises(ValueError, match="nonfinite"):
        inspect_splat(path)


def test_training_view_preserves_calibrated_projection_and_visible_depth(tmp_path):
    pycolmap = pytest.importorskip("pycolmap")
    np = pytest.importorskip("numpy")
    from services.vision.gaussian_pipeline import _view_camera

    (tmp_path / "cameras.txt").write_text("1 PINHOLE 800 600 640 620 390 280\n")
    # A rolled camera looking along world +Y, not along an assumed world axis.
    # Images 2/3 tie on support; deterministic name ordering chooses image 2.
    (tmp_path / "images.txt").write_text(
        "1 1 0 0 0 0 0 0 1 a.png\n390 280 2\n"
        "3 1 0 0 0 0 0 0 1 c.png\n710 435 1 390 280 2 390 280 3\n"
        "2 0.5 0.5 0.5 0.5 -5 -3 2 1 b.png\n710 435 1 390 280 2 390 280 3\n")
    (tmp_path / "points3D.txt").write_text(
        "1 4 2 7 255 255 255 0 2 0 3 0\n"
        "2 3 6 5 255 255 255 0 1 0 2 1 3 1\n"
        "3 3 -5 5 255 255 255 0 2 2 3 2\n")
    model = pycolmap.Reconstruction(str(tmp_path))
    view = _view_camera(model, str(tmp_path / "batch_1"))
    assert view is not None
    eye, forward, up = (np.array(view[key]) for key in ("eye", "forward", "up"))
    right = np.cross(forward, up)
    fx, fy, cx, cy, width, height = view["intrinsics"]
    for point, pixel in (([4, 2, 7], [710, 435]), ([3, 6, 5], [390, 280])):
        delta = np.array(point) - eye
        depth = delta @ forward
        projected = [fx * (delta @ right) / depth + cx, cy - fy * (delta @ up) / depth]
        assert projected == pytest.approx(pixel)
        assert 0 <= projected[0] < width and 0 <= projected[1] < height
    assert view["focus_distance"] == pytest.approx(6)  # Positive depths 4/8, excluding -3.
    assert _view_camera(model, str(tmp_path / "batch_1"))["id"] == view["id"]
    assert _view_camera(model, str(tmp_path / "batch_2"))["id"] != view["id"]


def _write_training_model(path, names):
    path.mkdir(parents=True)
    (path / "cameras.txt").write_text("1 PINHOLE 640 480 100 100 320 240\n")
    (path / "images.txt").write_text("".join(
        f"{i} 1 0 0 0 0 0 0 1 {name}\n320 240 1 360 240 2\n"
        for i, name in enumerate(names, 1)))
    observations = " ".join(f"{i} 0" for i in range(1, len(names) + 1))
    observations2 = " ".join(f"{i} 1" for i in range(1, len(names) + 1))
    (path / "points3D.txt").write_text(
        f"1 0 0 5 255 0 0 0 {observations}\n"
        f"2 2 0 5 0 255 0 0 {observations2}\n")


def test_prepared_dataset_preserves_sparse_gauge_and_source_names(tmp_path):
    pytest.importorskip("pycolmap")
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    from services.vision.colmap_pipeline import prepare_dataset, read_model

    source = tmp_path / "source"
    image_dir = source / "images"
    image_dir.mkdir(parents=True)
    names = ["a.png", "b.png", "c.png"]
    for name in names:
        assert cv2.imwrite(str(image_dir / name), np.full((480, 640, 3), 127, dtype=np.uint8))
    _write_training_model(source / "sparse", names)
    mapping = {name: "frames/" + name for name in names}
    result = prepare_dataset(source / "sparse", image_dir, tmp_path / "dataset",
                             source_image_map=mapping)
    assert result["ok"], result
    assert result["image_name_map"] == mapping
    assert (Path(result["sparse_dir"]) / "cameras.txt").is_file()
    _before_cameras, before_images, before_points = read_model(source / "sparse")
    _after_cameras, after_images, after_points = read_model(result["sparse_dir"])
    for image_id, image in before_images.items():
        assert after_images[image_id].qvec == pytest.approx(image.qvec)
        assert after_images[image_id].tvec == pytest.approx(image.tvec)
    for point_id, point in before_points.items():
        assert after_points[point_id].xyz == pytest.approx(point.xyz)


def test_existing_dataset_trains_without_resolving_and_retains_mapping(tmp_path, monkeypatch):
    pytest.importorskip("pycolmap")
    import json
    from services.vision import colmap_pipeline, dense_backends, gaussian_pipeline

    dataset = tmp_path / "dataset"
    images = dataset / "images"
    images.mkdir(parents=True)
    names = ["hash-a.png", "hash-b.png", "hash-c.png"]
    for name in names:
        (images / name).write_bytes(b"image")
    _write_training_model(dataset / "sparse", names)
    mapping = dict(zip(names, ["images/a.png", "frames/b.png", "frames/c.png"]))
    (dataset / "image_name_map.json").write_text(json.dumps(mapping))
    header, records = _ply()
    splat = tmp_path / "trained.ply"
    splat.write_bytes(header + b"".join(records))
    calls = []

    def no_resolve(*args, **kwargs):
        pytest.fail("training an existing dataset must not run SfM or undistortion")

    def brush(sparse, out, **kwargs):
        calls.append((sparse, out, kwargs))
        return {"ok": True, "splat_path": str(splat), "iteration": 20}

    monkeypatch.setattr(colmap_pipeline, "run_sparse", no_resolve)
    monkeypatch.setattr(colmap_pipeline, "prepare_dataset", no_resolve)
    monkeypatch.setattr(dense_backends, "run_brush", brush)
    result = gaussian_pipeline.train_splat_from_dataset(dataset, tmp_path / "work", iterations=20)
    assert result["ok"], result
    assert result["dataset_dir"] == str(dataset)
    assert result["image_name_map"] == mapping
    assert result["sparse_dir"] == str(dataset / "sparse")
    assert calls[0][0] == str(dataset / "sparse")
    assert calls[0][2]["image_dir"] == str(dataset)


def test_new_batch_persists_original_names_in_shared_dataset(tmp_path, monkeypatch):
    pytest.importorskip("pycolmap")
    import json
    import shutil
    from services.vision import colmap_pipeline, dense_backends, gaussian_pipeline

    session = tmp_path / "session"
    paths = [session / "images" / "same.png", session / "frames" / "same.png",
             session / "frames" / "third.png"]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image")
    header, records = _ply()
    splat = tmp_path / "trained.ply"
    splat.write_bytes(header + b"".join(records))

    def sparse(image_dir, out_dir, **kwargs):
        names = sorted(path.name for path in Path(image_dir).iterdir())
        target = Path(out_dir) / "sparse"
        _write_training_model(target, names)
        return {"ok": True, "registered": len(names), "sparse_dir": str(target)}

    def prepare(sparse_dir, image_dir, dataset_dir, *, source_image_map):
        dataset = Path(dataset_dir)
        shutil.copytree(sparse_dir, dataset / "sparse")
        shutil.copytree(image_dir, dataset / "images")
        mapping = dataset / "image_name_map.json"
        mapping.write_text(json.dumps(source_image_map))
        return {"ok": True, "sparse_dir": str(dataset / "sparse"),
                "image_name_map": source_image_map, "image_name_map_path": str(mapping)}

    monkeypatch.setattr(colmap_pipeline, "run_sparse", sparse)
    monkeypatch.setattr(colmap_pipeline, "prepare_dataset", prepare)
    monkeypatch.setattr(dense_backends, "run_brush",
                        lambda *args, **kwargs: {"ok": True, "splat_path": str(splat)})
    result = gaussian_pipeline.run({"work_dir": str(tmp_path / "work"),
                                    "session_dir": str(session),
                                    "images": [str(path) for path in paths]})
    assert result["ok"], result
    mapping = json.loads((tmp_path / "work" / "batch_image_map.json").read_text())
    assert set(mapping.values()) == {"images/same.png", "frames/same.png", "frames/third.png"}
    assert len(mapping) == 3
    assert result["image_name_map"] == mapping
