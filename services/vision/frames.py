"""Extract coverage-distributed, sharp video stills for reverse engineering.

OpenCV supplies the focus metric and JPEG encoder. Optional, user-installed ffprobe and
ffmpeg executables run out of process (see contracts/licenses.md); they are not bundled by
this adapter. ffprobe provides actual presentation timestamps, including for VFR footage,
and ffmpeg is the decoder fallback when OpenCV cannot open or decode the stream.

At most one full-size decoded image is retained at a time. Candidates are staged on disk,
then the sharpest above the blur floor in each temporal bin is published along with a
per-clip source manifest. By default the floor is relative to the clip's own median
sharpness: variance of the Laplacian scales with resolution, compression and scene
texture, so a fixed number rejects nearly every frame of ordinary phone video. An explicit
``blur_threshold`` is an absolute floor. Unknown-length sources are bounded by
``sample_cap`` and report truncation instead of pretending to cover an unseen tail.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fiducial import HAVE_CV2, VisionUnavailable, require_cv2

DEFAULT_BLUR_THRESHOLD: float = 100.0
#: Default video floor: a temporal bin's sharpest frame is dropped below this fraction of
#: the median sharpness of every sampled frame in the same clip.
RELATIVE_BLUR_FRACTION: float = 0.5
DEFAULT_MAX_FRAMES: int = 120
DEFAULT_SAMPLE_CAP: int = 600


def blur_score(frame) -> float:
    """Variance of the grayscale Laplacian; higher means sharper."""
    cv2 = require_cv2()
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


@dataclass
class FrameRecord:
    path: str
    frame_index: int
    time_s: float
    sharpness: float
    source_path: str = ""
    clip_id: str = ""
    orientation_degrees_clockwise: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "frame_index": self.frame_index,
            "time_s": self.time_s, "sharpness": self.sharpness,
            "blur_score": self.sharpness, "source_path": self.source_path,
            "clip_id": self.clip_id,
            "orientation_degrees_clockwise": self.orientation_degrees_clockwise,
        }


@dataclass
class FrameExtraction:
    frames: list[FrameRecord] = field(default_factory=list)
    sampled: int = 0
    rejected_blurry: int = 0
    total_frames: int = 0
    blur_threshold: float = DEFAULT_BLUR_THRESHOLD
    clip_id: str = ""
    source_path: str = ""
    count_known: bool = True
    truncated: bool = False
    sampling_limited: bool = False
    rejected_limit: int = 0
    sample_cap: int = DEFAULT_SAMPLE_CAP
    decoded: int = 0
    warnings: list[str] = field(default_factory=list)
    manifest_path: str = ""

    @property
    def kept(self) -> int:
        return len(self.frames)

    def to_dict(self) -> dict[str, Any]:
        return {
            "frames": [f.to_dict() for f in self.frames], "kept": self.kept,
            "sampled": self.sampled, "rejected_blurry": self.rejected_blurry,
            "total_frames": self.total_frames, "blur_threshold": self.blur_threshold,
            "clip_id": self.clip_id, "source_path": self.source_path,
            "count_known": self.count_known, "truncated": self.truncated,
            "sampling_limited": self.sampling_limited, "rejected_limit": self.rejected_limit,
            "sample_cap": self.sample_cap, "decoded": self.decoded,
            "warnings": self.warnings, "manifest_path": self.manifest_path,
        }


_FRAME_NAME = re.compile(r"clip_([0-9a-f]{64})_frame_\d{9}\.jpg")


def clip_camera_groups(source_by_name: dict[str, str]) -> list[list[str]]:
    """Group staged image names whose sources are frames of one extracted clip.

    A clip is recorded through one lens and zoom, so its frames share intrinsics; SfM
    with one camera per clip registers far more views than per-frame intrinsics.
    Sources not written by :func:`extract_frames` stay ungrouped.
    """
    groups: dict[str, list[str]] = {}
    for name, source in source_by_name.items():
        match = _FRAME_NAME.fullmatch(Path(source).name)
        if match:
            groups.setdefault(match.group(1), []).append(name)
    return list(groups.values())


def _positive_float(value: Any) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def probe_video(path: str, *, timeout: float = 3.0) -> dict[str, Any]:
    """Read inexpensive stream metadata; missing ffprobe or bad metadata yields {}.

    Rotation is the clockwise correction applied to the stored pixels. Duration and
    dimensions are omitted rather than guessed if the container does not report them.
    """
    binary = shutil.which("ffprobe")
    if not binary:
        return {}
    try:
        proc = subprocess.run(
            [binary, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,duration,nb_frames:stream_tags=rotate:"
             "stream_side_data=rotation:format=duration", "-of", "json", str(path)],
            capture_output=True, timeout=timeout, check=True,
        )
        data = json.loads(proc.stdout)
        stream = data.get("streams", [])[0]
        if not isinstance(stream, dict):
            return {}
        result: dict[str, Any] = {}
        for key in ("width", "height", "nb_frames"):
            value = _positive_float(stream.get(key))
            if value:
                result[key] = int(value)
        duration = _positive_float(stream.get("duration")) or _positive_float(
            data.get("format", {}).get("duration"))
        if duration:
            result["duration_s"] = duration
        for side in stream.get("side_data_list", []):
            if "rotation" in side:
                # ffprobe's display-matrix rotation is counterclockwise; OpenCV's
                # orientation metadata and cv2.rotate use the opposite convention.
                result["orientation_degrees_clockwise"] = int(round(-float(side["rotation"]))) % 360
                break
        else:
            if "rotate" in stream.get("tags", {}):
                result["orientation_degrees_clockwise"] = int(
                    round(float(stream["tags"]["rotate"]))) % 360
        return result
    except (OSError, ValueError, TypeError, IndexError, KeyError,
            subprocess.SubprocessError, json.JSONDecodeError):
        return {}


def _even_indices(total: int, cap: int) -> list[int]:
    if total <= 0:
        return []
    if total <= cap:
        return list(range(total))
    return [min(total - 1, int((k + 0.5) * total / cap)) for k in range(cap)]


def _probe_timestamps(path: Path, duration: float, limit: int):
    """Stream display-order timestamps and retain at most ``limit`` selected pairs.

    Temporal targets are used when a clip exceeds the candidate budget; shorter
    clips keep every frame. No unbounded list of source timestamps is held.
    """
    binary = shutil.which("ffprobe")
    if not binary:
        return None
    try:
        proc = subprocess.Popen(
            [binary, "-v", "error", "-select_streams", "v:0", "-show_frames",
             "-show_entries", "frame=best_effort_timestamp_time", "-of", "csv=p=0",
             str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except OSError:
        return None
    timer = threading.Timer(300.0, proc.kill)
    timer.daemon = True
    timer.start()
    selected: list[tuple[int, float]] = []
    short_clip: list[tuple[int, float]] = []
    previous = None
    first = None
    target = 0
    count = 0
    invalid = False
    first_interval = None
    last_interval = None
    variable_rate = False

    def add(item):
        if item is not None and (not selected or selected[-1][0] != item[0]):
            selected.append(item)

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            value = line.strip().split(",", 1)[0]
            if not value:
                continue
            try:
                pts = float(value)
                if not math.isfinite(pts):
                    raise ValueError("non-finite PTS")
            except ValueError:
                invalid = True
                break
            if first is None:
                first = pts
            timestamp = pts - first
            if timestamp < -1e-6 or (previous and timestamp < previous[1] - 1e-6):
                invalid = True
                break
            if previous is not None:
                interval = timestamp - previous[1]
                if interval > 0:
                    last_interval = interval
                if first_interval is None and interval > 0:
                    first_interval = interval
                elif first_interval is not None and abs(interval - first_interval) > max(0.0001, first_interval * 0.02):
                    variable_rate = True
            item = (count, max(0.0, timestamp))
            if len(short_clip) < limit:
                short_clip.append(item)
            if duration:
                while target < limit and item[1] >= (target + 0.5) * duration / limit:
                    t = (target + 0.5) * duration / limit
                    add(previous if previous and t - previous[1] <= item[1] - t else item)
                    target += 1
            previous = item
            count += 1
        if invalid:
            proc.kill()
        returncode = proc.wait()
        if invalid or returncode != 0 or not count:
            return None
        if count <= limit:
            inferred = previous[1] + (last_interval or 1.0)
            return short_clip, count, variable_rate, duration or inferred
        if not duration:
            # Some containers have neither duration nor frame count in their headers.
            # Discover the span in one streaming pass, then select by time in another.
            inferred = previous[1] + (last_interval or 1.0)
            timer.cancel()
            return _probe_timestamps(path, inferred, limit)
        if target < limit:
            add(previous)
        return selected, count, variable_rate, duration
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout:
            proc.stdout.close()


class _DecodeError(OSError):
    pass


def _rotate(frame, degrees: int):
    cv2 = require_cv2()
    if degrees == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if degrees == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if degrees == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if degrees != 0:
        raise ValueError(f"unsupported video orientation: {degrees} degrees")
    return frame


def _opencv_frames(cap, choices, total, fps, sample_cap, result, rotation, manual_rotation,
                   sequential):
    cv2 = require_cv2()
    def timestamp_for(index):
        current = float(cap.get(cv2.CAP_PROP_POS_MSEC) or 0) / 1000
        if math.isfinite(current) and current > 0:
            return current
        if fps:
            return index / fps
        if index == 0:
            return 0.0
        raise _DecodeError("video has no usable timestamps or frame rate")

    if total:
        next_index = 0
        for index, timestamp in choices:
            if sequential:
                # Seeking frame numbers in VFR files is often implemented through an
                # average FPS and can return a different frame than the manifest PTS.
                while next_index <= index:
                    ok, frame = cap.read()
                    if not ok or frame is None or frame.size == 0:
                        raise _DecodeError(f"could not decode source frame {next_index}")
                    result.decoded += 1
                    next_index += 1
            else:
                if not cap.set(cv2.CAP_PROP_POS_FRAMES, index):
                    raise _DecodeError(f"could not seek to source frame {index}")
                ok, frame = cap.read()
                if not ok or frame is None or frame.size == 0:
                    raise _DecodeError(f"could not decode source frame {index}")
                result.decoded += 1
            if timestamp is None:
                timestamp = timestamp_for(index)
            yield index, timestamp, _rotate(frame, rotation) if manual_rotation else frame
    else:
        for index in range(sample_cap + 1):
            ok, frame = cap.read()
            if not ok:
                if index == 0:
                    raise _DecodeError("video contains no decodable frames")
                break
            if frame is None or frame.size == 0:
                raise _DecodeError(f"could not decode source frame {index}")
            result.decoded += 1
            if index == sample_cap:
                result.truncated = result.sampling_limited = True
                break
            timestamp = timestamp_for(index)
            yield index, timestamp, _rotate(frame, rotation) if manual_rotation else frame
        result.total_frames = result.decoded
        result.warnings.append(
            f"source frame count unavailable; decoded {result.decoded} frames"
            + (f"; truncated sampling at {sample_cap} candidates" if result.truncated else "; decoder reached end of stream")
            + "; source length and decoder EOF cannot be independently verified")


def _ffmpeg_frames(src, choices, width, height, rotation, result):
    import numpy as np

    binary = shutil.which("ffmpeg")
    if not binary or not choices or not width or not height:
        raise _DecodeError("ffmpeg fallback requires user-installed ffmpeg/ffprobe and readable video metadata")
    # Select by presentation-order frame index, not a guessed CFR seek timestamp.
    select = "+".join(f"eq(n\\,{index})" for index, _ in choices)
    cmd = [binary, "-v", "error", "-noautorotate", "-i", str(src), "-map", "0:v:0",
           "-vf", "select=" + select, "-vsync", "0", "-pix_fmt", "bgr24",
           "-threads", "1", "-f", "rawvideo", "pipe:1"]
    with tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors)
        except OSError as exc:
            raise _DecodeError(f"could not start ffmpeg: {exc}") from exc
        try:
            assert proc.stdout is not None
            size = width * height * 3
            for index, timestamp in choices:
                data = bytearray()
                while len(data) < size:
                    chunk = proc.stdout.read(size - len(data))
                    if not chunk:
                        break
                    data.extend(chunk)
                if len(data) != size:
                    raise _DecodeError(f"ffmpeg could not decode source frame {index}")
                frame = np.frombuffer(data, dtype=np.uint8).reshape((height, width, 3))
                result.decoded += 1
                yield index, timestamp, _rotate(frame, rotation)
            if proc.wait() != 0:
                errors.seek(0)
                raise _DecodeError("ffmpeg decode failed: " + errors.read(2048).decode("utf-8", "replace"))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdout:
                proc.stdout.close()


def extract_frames(video_path: str, out_dir: str, *, max_frames: int = DEFAULT_MAX_FRAMES,
                   blur_threshold: float | None = None,
                   sample_cap: int = DEFAULT_SAMPLE_CAP, jpeg_quality: int = 92) -> FrameExtraction:
    """Extract at most one sharp still per temporal bin and persist its source mapping.

    ``blur_threshold=None`` uses ``RELATIVE_BLUR_FRACTION`` of the clip's median sampled
    sharpness; a number is an absolute variance-of-Laplacian floor. The manifest's
    ``blur_threshold`` reports the floor actually applied.
    """
    for name, value in (("max_frames", max_frames), ("sample_cap", sample_cap)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if isinstance(jpeg_quality, bool) or not isinstance(jpeg_quality, int) or not 1 <= jpeg_quality <= 100:
        raise ValueError("jpeg_quality must be an integer between 1 and 100")
    relative = blur_threshold is None
    if not relative and (not math.isfinite(blur_threshold) or blur_threshold < 0):
        raise ValueError("blur_threshold must be finite and nonnegative")
    cv2 = require_cv2()
    src = Path(video_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"no such video file: {video_path}")
    with src.open("rb") as stream:
        clip_id = hashlib.file_digest(stream, "sha256").hexdigest()
    meta = probe_video(str(src))
    cap = cv2.VideoCapture(str(src))
    opened = cap.isOpened()
    total = int(_positive_float(cap.get(cv2.CAP_PROP_FRAME_COUNT))) if opened else 0
    total = total or int(meta.get("nb_frames", 0))
    fps = _positive_float(cap.get(cv2.CAP_PROP_FPS)) if opened else 0
    duration = meta.get("duration_s", 0)
    rotation = meta.get("orientation_degrees_clockwise")
    orientation_prop = getattr(cv2, "CAP_PROP_ORIENTATION_META", None)
    if rotation is None and opened and orientation_prop is not None:
        rotation = int(round(cap.get(orientation_prop) or 0)) % 360
    rotation = rotation or 0
    manual_rotation = True
    auto_prop = getattr(cv2, "CAP_PROP_ORIENTATION_AUTO", None)
    if opened and auto_prop is not None and hasattr(cap, "set"):
        manual_rotation = bool(cap.set(auto_prop, 0))
    timing = _probe_timestamps(src, duration, sample_cap) if meta else None
    if timing:
        choices, total, variable_rate, duration = timing
    else:
        choices = [(i, None) for i in _even_indices(total, sample_cap)]
        variable_rate = False
        duration = duration or (total / fps if total and fps else 0)
    result = FrameExtraction(blur_threshold=0.0 if relative else blur_threshold, clip_id=clip_id,
                             source_path=str(src), sample_cap=sample_cap,
                             total_frames=total, count_known=total > 0,
                             sampling_limited=total > sample_cap)
    if not timing and total:
        result.warnings.append("frame timestamps are decoder-reported or estimated from frame rate; VFR timing may be imprecise")
    out = Path(out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    result.manifest_path = str(out / f"clip_{clip_id}_manifest.json")
    try:
        with tempfile.TemporaryDirectory(prefix=".video-", dir=out) as staging:
            stage = Path(staging)

            def collect(iterator):
                # Unknown duration needs candidates on disk until the observed span is known.
                candidates = []
                bins = {}
                scores = []
                last_timestamp = 0.0
                for index, timestamp, frame in iterator:
                    result.sampled += 1
                    last_timestamp = max(last_timestamp, timestamp)
                    sharpness = blur_score(frame)
                    if not math.isfinite(sharpness):
                        raise ValueError(f"invalid sharpness for source frame {index}")
                    scores.append(sharpness)
                    if not relative and sharpness < blur_threshold:
                        result.rejected_blurry += 1
                        continue
                    slot = min(max_frames - 1, int(max(0.0, timestamp) * max_frames / duration)) if duration else None
                    current = bins.get(slot) if slot is not None else None
                    if current is not None and sharpness <= current[2]:
                        continue
                    path = stage / f"{index:09d}.jpg"
                    if not cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]):
                        raise OSError(f"could not write extracted source frame {index}")
                    if not path.is_file() or path.stat().st_size == 0:
                        raise OSError(f"encoder produced no data for source frame {index}")
                    item = (index, timestamp, sharpness, path)
                    if slot is None:
                        candidates.append(item)
                    else:
                        if current is not None:
                            current[3].unlink()
                        bins[slot] = item
                if not duration and candidates:
                    # Include the last frame's interval so it belongs to the final bin.
                    interval = 1.0 / fps if fps else (
                        last_timestamp / max(result.sampled - 1, 1) or 1.0)
                    span = max(last_timestamp + interval, interval)
                    for item in candidates:
                        slot = min(max_frames - 1, int(item[1] * max_frames / span))
                        current = bins.get(slot)
                        if current is None or item[2] > current[2]:
                            bins[slot] = item
                if relative and scores:
                    # The clip median is known only after sampling; drop bins whose sharpest
                    # frame is still well below it rather than gating on an absolute number.
                    floor = RELATIVE_BLUR_FRACTION * statistics.median(scores)
                    result.blur_threshold = round(floor, 3)
                    result.rejected_blurry = sum(score < floor for score in scores)
                    for slot, item in list(bins.items()):
                        if item[2] < floor:
                            item[3].unlink()
                            del bins[slot]
                return sorted(bins.values(), key=lambda item: item[0])

            try:
                if not opened:
                    raise _DecodeError("OpenCV could not open video")
                selected = collect(_opencv_frames(cap, choices, total, fps, sample_cap,
                                                  result, rotation, manual_rotation,
                                                  variable_rate))
            except _DecodeError as exc:
                cap.release()
                if not timing:
                    raise _DecodeError(f"{exc}; ffprobe could not provide fallback frame timing") from exc
                for file in stage.iterdir():
                    file.unlink()
                result.sampled = result.rejected_blurry = result.decoded = 0
                result.truncated = False
                result.warnings.append(f"OpenCV decoding failed ({exc}); used ffmpeg subprocess")
                selected = collect(_ffmpeg_frames(src, choices, meta.get("width", 0),
                                                  meta.get("height", 0), rotation, result))
            result.rejected_limit = result.sampled - result.rejected_blurry - len(selected)
            if result.sampling_limited and result.count_known:
                result.warnings.append(f"sampled {result.sampled} of {result.total_frames} source frames")
            if result.rejected_limit:
                result.warnings.append(f"discarded {result.rejected_limit} sharp candidates in occupied temporal bins")
            for index, timestamp, sharpness, temporary in selected:
                path = out / f"clip_{clip_id}_frame_{index:09d}.jpg"
                result.frames.append(FrameRecord(
                    path=str(path), frame_index=index, time_s=round(timestamp, 6),
                    sharpness=round(sharpness, 3), source_path=str(src), clip_id=clip_id,
                    orientation_degrees_clockwise=rotation))
            # Finish all encoding and manifest serialization before publishing anything.
            manifest = stage / "manifest.json"
            manifest.write_text(json.dumps({"version": 1, **result.to_dict()}, indent=2) + "\n", encoding="utf-8")
            for item, frame in zip(selected, result.frames):
                os.replace(item[3], frame.path)
            os.replace(manifest, result.manifest_path)
    finally:
        cap.release()
    return result


def extract_frames_safe(video_path: str, out_dir: str, **kw) -> dict[str, Any]:
    """Contract-shaped wrapper: return ``{ok, ...}`` rather than raising."""
    if not HAVE_CV2:
        e = VisionUnavailable()
        return {"ok": False, "reason": e.reason, "hint": e.hint}
    try:
        res = extract_frames(video_path, out_dir, **kw)
    except VisionUnavailable as e:
        return {"ok": False, "reason": e.reason, "hint": e.hint}
    except FileNotFoundError as e:
        return {"ok": False, "reason": str(e), "hint": "check the video path and codec support"}
    except (ValueError, TypeError) as e:
        return {"ok": False, "reason": str(e), "hint": "check frame limits and extraction parameters"}
    except Exception as e:
        return {"ok": False, "reason": str(e), "hint": "check video decoding and output directory write access"}
    return {"ok": True, **res.to_dict()}
