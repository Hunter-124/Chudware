"""Consumer contracts: Gaussian crops preserve payloads and refuse corrupt data."""
from __future__ import annotations

import struct

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
