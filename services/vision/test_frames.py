"""Tests for :mod:`services.vision.frames` — video ingest + blur cull.

The OpenCV-backed tests ``importorskip("cv2")`` so the suite stays green on a box that hasn't
provisioned OpenCV (same convention as ``test_vision.py``). A synthetic video is written with
``cv2.VideoWriter``: alternating *sharp* frames (a crisp high-contrast checker/box) and *blurred*
frames (the same content run through a heavy Gaussian blur). The extractor must keep the sharp
ones and reject the blurred ones — that is the whole contract of this module.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ..vision import frames as F

# --------------------------------------------------------------- no-cv2 degradation (import-safe)


def test_extract_frames_safe_degrades_without_cv2(monkeypatch, tmp_path):
    """Without OpenCV the safe wrapper returns a contract-shaped error, never a traceback."""
    monkeypatch.setattr(F, "HAVE_CV2", False)
    out = F.extract_frames_safe("nope.mp4", str(tmp_path / "frames"))
    assert out["ok"] is False
    assert "reason" in out and "hint" in out


def test_missing_video_is_contract_error(tmp_path):
    """A missing path is reported, not raised, through the safe wrapper (cv2 present or not)."""
    if not F.HAVE_CV2:
        pytest.skip("cv2 not installed; degradation path covered above")
    out = F.extract_frames_safe(str(tmp_path / "does_not_exist.mp4"), str(tmp_path / "f"))
    assert out["ok"] is False
    assert "reason" in out


# ------------------------------------------------------------------ synthetic-video blur cull


def _make_frame(cv2, np, w, h, blurred: bool, shift: int):
    """A high-contrast checkerboard; the blurred variant is heavily Gaussian-softened."""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    tile = 24
    for y in range(0, h, tile):
        for x in range(0, w, tile):
            if ((x // tile) + (y // tile) + shift) % 2 == 0:
                img[y : y + tile, x : x + tile] = 255
    if blurred:
        img = cv2.GaussianBlur(img, (31, 31), 0)
    return img


@pytest.fixture
def synthetic_video(tmp_path):
    """Write a 20-frame video alternating sharp/blurred; return (path, n_sharp, n_blur)."""
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    w, h, fps, n = 160, 120, 10, 20
    path = tmp_path / "clip.avi"
    fourcc = cv2.VideoWriter_fourcc(*"FFV1")  # lossless so blur separation survives encoding
    vw = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
    if not vw.isOpened():  # fall back to a widely-available codec
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        vw = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
    assert vw.isOpened(), "no usable VideoWriter codec on this box"
    n_sharp = n_blur = 0
    for i in range(n):
        blurred = (i % 2 == 1)
        vw.write(_make_frame(cv2, np, w, h, blurred, shift=i))
        if blurred:
            n_blur += 1
        else:
            n_sharp += 1
    vw.release()
    assert path.is_file() and path.stat().st_size > 0
    return path, n_sharp, n_blur


def test_blur_cull_keeps_sharp_rejects_blurred(synthetic_video, tmp_path):
    path, n_sharp, n_blur = synthetic_video
    out_dir = tmp_path / "frames"
    res = F.extract_frames(str(path), str(out_dir), blur_threshold=F.DEFAULT_BLUR_THRESHOLD)

    # every candidate was scored; nothing silently vanished
    assert res.sampled == res.kept + res.rejected_blurry
    # the blurred frames were culled
    assert res.rejected_blurry >= n_blur - 1  # allow 1 for codec keyframe fuzz
    # the sharp frames survived and were written to disk
    assert res.kept >= n_sharp - 1
    assert res.kept > 0
    written = sorted(out_dir.glob("clip_*_frame_*.jpg"))
    assert len(written) == res.kept
    for p in written:
        assert p.stat().st_size > 0
    # every kept frame clears the threshold
    for fr in res.frames:
        assert fr.sharpness >= F.DEFAULT_BLUR_THRESHOLD


def test_max_frames_cap_keeps_sharpest(synthetic_video, tmp_path):
    path, n_sharp, _ = synthetic_video
    out_dir = tmp_path / "capped"
    cap = 3
    res = F.extract_frames(str(path), str(out_dir), max_frames=cap,
                           blur_threshold=F.DEFAULT_BLUR_THRESHOLD)
    assert res.kept <= cap
    assert len(list(out_dir.glob("clip_*_frame_*.jpg"))) == res.kept
    # kept frames are emitted in source-time order
    idxs = [fr.frame_index for fr in res.frames]
    assert idxs == sorted(idxs)




def test_clip_identity_retains_two_clips_and_repeat_import(synthetic_video, tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    first_path, _, _ = synthetic_video
    second_path = tmp_path / "second.avi"
    writer = cv2.VideoWriter(str(second_path), cv2.VideoWriter_fourcc(*"FFV1"),
                             10, (160, 120))
    assert writer.isOpened()
    for idx in range(20):
        image = _make_frame(cv2, np, 160, 120, False, idx)
        image[0:16, 0:16] = (0, 0, 255)
        writer.write(image)
    writer.release()
    output = tmp_path / "shared"
    first = F.extract_frames(str(first_path), str(output), max_frames=3)
    original = {row.path: Path(row.path).read_bytes() for row in first.frames}
    second = F.extract_frames(str(second_path), str(output), max_frames=3)
    repeated = F.extract_frames(str(first_path), str(output), max_frames=3)
    assert set(original).isdisjoint(row.path for row in second.frames)
    assert [row.path for row in repeated.frames] == [row.path for row in first.frames]
    assert len(list(output.glob("*.jpg"))) == first.kept + second.kept
    assert {path: Path(path).read_bytes() for path in original} == original


def test_unknown_length_sampling_is_bounded_and_reports_truncation(monkeypatch, tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    source = tmp_path / "unknown.avi"
    source.write_bytes(b"unknown-length-decoder-fixture")
    image = _make_frame(cv2, np, 160, 120, False, 0)

    class UnknownCapture:
        reads = 0
        released = False

        def isOpened(self):
            return True

        def get(self, prop):
            return 10 if prop == cv2.CAP_PROP_FPS else 0

        def read(self):
            self.reads += 1
            # An unbounded source must never be drained into memory.
            assert self.reads <= 8
            return True, image.copy()

        def release(self):
            self.released = True

    capture = UnknownCapture()
    monkeypatch.setattr(cv2, "VideoCapture", lambda _: capture)
    result = F.extract_frames(str(source), str(tmp_path / "frames"), sample_cap=7, max_frames=2)
    assert capture.released
    assert capture.reads == 8
    assert result.sampled == 7
    assert result.kept == 2
    assert result.rejected_limit == 5
    assert result.truncated and not result.count_known
    assert result.to_dict()["warnings"]


@pytest.mark.parametrize("limits", [{"max_frames": 0}, {"sample_cap": -1}, {"sample_cap": 2.5}])
def test_invalid_limits_refused_before_decoding(limits, tmp_path):
    with pytest.raises(ValueError):
        F.extract_frames("unused.mp4", str(tmp_path), **limits)


def test_write_failure_publishes_no_frames(monkeypatch, synthetic_video, tmp_path):
    cv2 = pytest.importorskip("cv2")
    path, _, _ = synthetic_video
    output = tmp_path / "failed"
    monkeypatch.setattr(cv2, "imwrite", lambda *args: False)
    result = F.extract_frames_safe(str(path), str(output))
    assert result["ok"] is False
    assert not list(output.glob("*.jpg"))


def test_known_length_decode_failure_is_not_silent_cull(monkeypatch, synthetic_video, tmp_path):
    cv2 = pytest.importorskip("cv2")
    path, _, _ = synthetic_video
    real_capture = cv2.VideoCapture

    class BrokenCapture:
        def __init__(self, path):
            self.capture = real_capture(path)
            self.reads = 0

        def isOpened(self):
            return self.capture.isOpened()

        def get(self, prop):
            return self.capture.get(prop)

        def set(self, prop, value):
            return self.capture.set(prop, value)

        def read(self):
            self.reads += 1
            return (False, None) if self.reads == 2 else self.capture.read()

        def release(self):
            self.capture.release()

    monkeypatch.setattr(cv2, "VideoCapture", BrokenCapture)
    output = tmp_path / "broken"
    result = F.extract_frames_safe(str(path), str(output))
    assert result["ok"] is False
    assert not list(output.glob("*.jpg"))
