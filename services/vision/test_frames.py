"""Tests for :mod:`services.vision.frames` — video ingest + blur cull.

The OpenCV-backed tests ``importorskip("cv2")`` so the suite stays green on a box that hasn't
provisioned OpenCV (same convention as ``test_vision.py``). A synthetic video is written with
``cv2.VideoWriter``: alternating *sharp* frames (a crisp high-contrast checker/box) and *blurred*
frames (the same content run through a heavy Gaussian blur). The extractor must keep the sharp
ones and reject the blurred ones — that is the whole contract of this module.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import weakref
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
    if not vw.isOpened():
        pytest.skip("no usable OpenCV VideoWriter codec")
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


def test_default_floor_is_relative_to_the_clip(tmp_path):
    """Phone video scores far below the absolute floor; only clip-relative smear is culled."""
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    path = tmp_path / "soft.avi"
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"FFV1"), 10, (160, 120))
    if not vw.isOpened():
        pytest.skip("no lossless OpenCV VideoWriter codec")
    for i in range(20):
        frame = _make_frame(cv2, np, 160, 120, False, i % 2) // 6 + 100
        frame = cv2.GaussianBlur(frame, (31, 31) if i >= 15 else (5, 5), 0)
        vw.write(frame)
    vw.release()

    absolute = F.extract_frames(str(path), str(tmp_path / "absolute"), max_frames=4,
                                blur_threshold=F.DEFAULT_BLUR_THRESHOLD)
    assert absolute.kept == 0  # precondition: every frame is "blurry" in absolute terms
    res = F.extract_frames(str(path), str(tmp_path / "relative"), max_frames=4)
    assert [min(3, frame.frame_index // 5) for frame in res.frames] == [0, 1, 2]
    assert res.rejected_blurry == 5
    manifest = json.loads(Path(res.manifest_path).read_text(encoding="utf-8"))
    assert 0 < manifest["blur_threshold"] < F.DEFAULT_BLUR_THRESHOLD
    assert all(frame.sharpness >= manifest["blur_threshold"] for frame in res.frames)
    assert len(list((tmp_path / "relative").glob("clip_*_frame_*.jpg"))) == 3


def test_clip_identity_retains_two_clips_and_repeat_import(synthetic_video, tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    first_path, _, _ = synthetic_video
    second_path = tmp_path / "second.avi"
    writer = cv2.VideoWriter(str(second_path), cv2.VideoWriter_fourcc(*"FFV1"),
                             10, (160, 120))
    if not writer.isOpened():
        pytest.skip("FFV1 encoder unavailable")
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
    # Exercise the fail-closed path, not the separately tested ffmpeg fallback.
    monkeypatch.setattr(F, "_probe_timestamps", lambda *args: None)
    output = tmp_path / "broken"
    result = F.extract_frames_safe(str(path), str(output))
    assert result["ok"] is False
    assert not list(output.glob("*.jpg"))


def _ffmpeg_clip(tmp_path, extension, codec, *, frames=None):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are optional")
    if frames is None:
        frames = [_make_frame(cv2, np, 160, 120, False, i % 2) for i in range(24)]
    path = tmp_path / f"source.{extension}"
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", "160x120", "-r", "10", "-i", "pipe:0", "-an", "-c:v", codec,
         "-threads", "1", str(path)],
        input=b"".join(frame.tobytes() for frame in frames), capture_output=True,
    )
    if proc.returncode:
        pytest.skip(f"{codec} encoder or {extension} muxer unavailable: "
                    + proc.stderr.decode("utf-8", "replace")[:200])
    return path


@pytest.mark.parametrize(("extension", "codec"), [
    ("mp4", "mpeg4"), ("mov", "mpeg4"), ("webm", "libvpx"), ("mkv", "ffv1"),
])
def test_supported_video_containers_have_temporal_coverage_and_manifest(tmp_path, extension, codec):
    path = _ffmpeg_clip(tmp_path, extension, codec)
    result = F.extract_frames(str(path), str(tmp_path / "frames"),
                              max_frames=4, sample_cap=16)
    assert result.sampled <= 16
    assert result.kept == 4
    duration = F.probe_video(str(path))["duration_s"]
    assert [min(3, int(frame.time_s * 4 / duration)) for frame in result.frames] == [0, 1, 2, 3]
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert manifest["frames"] == result.to_dict()["frames"]
    for record in manifest["frames"]:
        assert record["source_path"] == str(path.resolve())
        assert record["clip_id"] == result.clip_id
        assert record["orientation_degrees_clockwise"] == 0
        assert Path(record["path"]).is_file()


def test_selects_sharpest_candidate_in_each_time_bin(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    images = []
    for idx in range(24):
        image = _make_frame(cv2, np, 160, 120, False, idx % 2)
        if idx % 3:
            image = cv2.GaussianBlur(image, (5 if idx % 3 == 1 else 9,) * 2, 0)
        images.append(image)
    video = _ffmpeg_clip(tmp_path, "mp4", "mpeg4", frames=images)
    all_frames = F.extract_frames(str(video), str(tmp_path / "all"), max_frames=24,
                                  sample_cap=24, blur_threshold=0)
    selected = F.extract_frames(str(video), str(tmp_path / "best"), max_frames=4,
                                sample_cap=24, blur_threshold=0)
    duration = F.probe_video(str(video))["duration_s"]
    assert selected.kept == 4
    for frame in selected.frames:
        slot = min(3, int(frame.time_s * 4 / duration))
        candidates = [row for row in all_frames.frames
                      if min(3, int(row.time_s * 4 / duration)) == slot]
        assert frame.sharpness == max(row.sharpness for row in candidates)


def test_full_resolution_candidate_images_do_not_accumulate(tmp_path, monkeypatch):
    video = _ffmpeg_clip(tmp_path, "mp4", "mpeg4")
    references = []
    peak_live = 0
    original = F.blur_score

    def checked_score(frame):
        nonlocal peak_live
        references.append(weakref.ref(frame))
        peak_live = max(peak_live, sum(ref() is not None for ref in references))
        return original(frame)

    monkeypatch.setattr(F, "blur_score", checked_score)
    result = F.extract_frames(str(video), str(tmp_path / "bounded"), max_frames=12,
                              sample_cap=24, blur_threshold=0)
    assert result.sampled == 24 and result.kept == 12
    assert peak_live <= 2


def test_ffmpeg_fallback_when_opencv_cannot_open(tmp_path, monkeypatch):
    cv2 = pytest.importorskip("cv2")
    path = _ffmpeg_clip(tmp_path, "mp4", "mpeg4")

    class ClosedCapture:
        def isOpened(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(cv2, "VideoCapture", lambda _: ClosedCapture())
    result = F.extract_frames(str(path), str(tmp_path / "fallback"), max_frames=4,
                              sample_cap=16)
    assert result.kept == 4
    assert any("used ffmpeg subprocess" in warning for warning in result.warnings)
    assert all(Path(frame.path).stat().st_size > 0 for frame in result.frames)


def test_ffmpeg_fallback_without_reported_duration_or_count(tmp_path, monkeypatch):
    cv2 = pytest.importorskip("cv2")
    path = _ffmpeg_clip(tmp_path, "mkv", "ffv1")
    real_probe = F.probe_video

    class ClosedCapture:
        def isOpened(self):
            return False

        def release(self):
            pass

    def no_length(path, **kwargs):
        metadata = real_probe(path, **kwargs)
        metadata.pop("duration_s", None)
        metadata.pop("nb_frames", None)
        return metadata

    monkeypatch.setattr(cv2, "VideoCapture", lambda _: ClosedCapture())
    monkeypatch.setattr(F, "probe_video", no_length)
    result = F.extract_frames(str(path), str(tmp_path / "no-length"), max_frames=4,
                              sample_cap=16)
    assert result.count_known and result.total_frames == 24
    assert result.kept == 4
    assert result.frames[0].time_s < 0.6
    assert result.frames[-1].time_s > 1.8


def test_rotation_metadata_applied_once_for_both_decoders(tmp_path, monkeypatch):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    frame = _make_frame(cv2, np, 160, 120, False, 0)
    frame[:40, :50] = (0, 0, 255)
    source = _ffmpeg_clip(tmp_path, "mp4", "mpeg4", frames=[frame] * 12)
    rotated = tmp_path / "rotated.mov"
    proc = subprocess.run(
        [shutil.which("ffmpeg"), "-v", "error", "-y", "-i", str(source), "-c", "copy",
         "-metadata:s:v:0", "rotate=90", str(rotated)], capture_output=True)
    if proc.returncode or not F.probe_video(str(rotated)).get("orientation_degrees_clockwise"):
        pytest.skip("ffmpeg could not write rotation metadata")
    reference = subprocess.run(
        [shutil.which("ffmpeg"), "-v", "error", "-i", str(rotated), "-frames:v", "1",
         "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
        capture_output=True)
    if reference.returncode:
        pytest.skip("ffmpeg PNG encoder unavailable")
    expected = cv2.imdecode(np.frombuffer(reference.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
    normal = F.extract_frames(str(rotated), str(tmp_path / "rot-normal"), max_frames=3)

    class ClosedCapture:
        def isOpened(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(cv2, "VideoCapture", lambda _: ClosedCapture())
    fallback = F.extract_frames(str(rotated), str(tmp_path / "rot-fallback"), max_frames=3)
    assert normal.kept and fallback.kept
    for result in (normal, fallback):
        actual = cv2.imread(result.frames[0].path)
        assert actual.shape == expected.shape
        assert actual.shape[0] > actual.shape[1]
        assert np.abs(actual.astype("int16") - expected.astype("int16")).mean() < 15
        assert result.frames[0].orientation_degrees_clockwise in (90, 270)


def test_variable_frame_rate_uses_display_timestamps(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg and ffprobe are optional")
    concat = []
    for i, duration in enumerate((0.2, 0.8, 0.3, 1.0)):
        image = tmp_path / f"vfr{i}.png"
        cv2.imwrite(str(image), _make_frame(cv2, np, 160, 120, False, i))
        concat.extend([f"file '{image.as_posix()}'", f"duration {duration}"])
    concat.append(f"file '{image.as_posix()}'")
    listing = tmp_path / "concat.txt"
    listing.write_text("\n".join(concat) + "\n", encoding="utf-8")
    video = tmp_path / "variable.mp4"
    encoded = subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-vsync", "vfr", "-c:v", "mpeg4", "-threads", "1", str(video)], capture_output=True)
    if encoded.returncode:
        pytest.skip("VFR MPEG-4 encoder unavailable")
    raw = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
         "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(video)],
        capture_output=True, check=True)
    timestamps = [float(frame["best_effort_timestamp_time"])
                  for frame in json.loads(raw.stdout)["frames"]]
    assert len(set(round(b - a, 2) for a, b in zip(timestamps, timestamps[1:]))) > 1
    result = F.extract_frames(str(video), str(tmp_path / "vfr"), max_frames=4,
                              sample_cap=20, blur_threshold=0)
    assert result.kept >= 3
    for frame in result.frames:
        assert frame.time_s == pytest.approx(timestamps[frame.frame_index] - timestamps[0], abs=1e-5)
        expected = _make_frame(cv2, np, 160, 120, False, min(frame.frame_index, 3))
        actual = cv2.imread(frame.path)
        assert np.abs(actual.astype("int16") - expected.astype("int16")).mean() < 15
