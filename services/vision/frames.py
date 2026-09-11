"""Video → blur-culled stills — the capture front-end for the Reverse Engineering pipeline.

A user hands the RE workspace a phone video that orbits the part; photogrammetry / 3DGS want
a handful of *sharp*, well-spread stills, not every motion-blurred frame. This module extracts
frames on an even time grid, scores each by **focus** (variance of the Laplacian — the classic
Pech-Pacheco no-reference sharpness metric), culls the blurry ones, and writes the survivors
into the session ``frames/`` directory (contract ``reveng.md`` §1 layout).

``cv2`` (Apache-2.0) is optional: this module imports without it and reuses
:func:`~services.vision.fiducial.require_cv2` / :class:`~services.vision.fiducial.VisionUnavailable`
so a box without OpenCV degrades to a contract-shaped ``{ok: False, reason, hint}`` instead of a
traceback. No new dependency is introduced — OpenCV is already the vision-module runtime dep.

Design (both knobs are explicit parameters, defaults documented — no silent magic):

* **sample-then-cull.** With a trustworthy frame count, sample up to ``sample_cap``
  candidates evenly across the video; unknown-length decoding stops at that cap
  plus one lookahead and explicitly reports truncation. Drop candidates below
  ``blur_threshold`` and retain the ``max_frames`` sharpest. Only those best frames
  are retained in memory, never the whole video.
* **absolute Laplacian-variance threshold.** ``blur_threshold`` is scene-dependent in general;
  the default (:data:`DEFAULT_BLUR_THRESHOLD`) is a sane starting point for phone video and is a
  parameter the caller (or a later adaptive pass) can override. A synthetic fixture controls the
  sharp-vs-blurred separation directly, so the fixed default is exercised deterministically.
"""
from __future__ import annotations

import hashlib
import heapq
import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fiducial import HAVE_CV2, VisionUnavailable, require_cv2

#: Below this variance-of-Laplacian a frame is judged too blurry to keep. Tuned for downscaled
#: phone video; override per-session if a scene runs consistently softer/sharper.
DEFAULT_BLUR_THRESHOLD: float = 100.0

#: Contract default (``reveng_add_video(..., max_frames=120)``): most stills we keep.
DEFAULT_MAX_FRAMES: int = 120

#: Candidate decode/score budget; known-length sampling spans the whole video.
#: Unknown-length decoding uses at most one additional frame to report truncation.
DEFAULT_SAMPLE_CAP: int = 600


def blur_score(frame) -> float:
    """Focus metric for one BGR frame: variance of its Laplacian (higher = sharper).

    Grayscale first so the metric is illumination/edge based, not colour based. Raises
    :class:`VisionUnavailable` if OpenCV is not installed.
    """
    cv2 = require_cv2()
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


@dataclass
class FrameRecord:
    """One kept still: where it landed on disk + why it survived the cull."""

    path: str                 # absolute path to the written still (JPEG)
    frame_index: int          # source frame index in the original video
    time_s: float             # timestamp (seconds) of that frame in the source
    sharpness: float          # variance-of-Laplacian focus score
    source_path: str = ""
    clip_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "frame_index": self.frame_index,
            "time_s": self.time_s,
            "sharpness": self.sharpness,
            "blur_score": self.sharpness,
            "source_path": self.source_path,
            "clip_id": self.clip_id,
        }


@dataclass
class FrameExtraction:
    """Result of :func:`extract_frames`: kept stills + honest cull accounting."""

    frames: list[FrameRecord] = field(default_factory=list)
    sampled: int = 0          # candidate frames decoded + scored
    rejected_blurry: int = 0  # scored candidates dropped below threshold
    total_frames: int = 0     # frame count reported by the source video
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

    @property
    def kept(self) -> int:
        return len(self.frames)

    def to_dict(self) -> dict[str, Any]:
        return {
            "frames": [f.to_dict() for f in self.frames],
            "kept": self.kept,
            "sampled": self.sampled,
            "rejected_blurry": self.rejected_blurry,
            "total_frames": self.total_frames,
            "blur_threshold": self.blur_threshold,
            "clip_id": self.clip_id,
            "source_path": self.source_path,
            "count_known": self.count_known,
            "truncated": self.truncated,
            "sampling_limited": self.sampling_limited,
            "rejected_limit": self.rejected_limit,
            "sample_cap": self.sample_cap,
            "decoded": self.decoded,
            "warnings": self.warnings,
        }


def _even_indices(total: int, cap: int) -> list[int]:
    """Up to ``cap`` frame indices spread evenly across ``[0, total)``."""
    if total <= 0:
        return []
    if cap <= 0 or total <= cap:
        return list(range(total))
    step = total / float(cap)
    # round to nearest, dedupe while preserving order
    seen: set[int] = set()
    out: list[int] = []
    for k in range(cap):
        idx = min(total - 1, int(round(k * step)))
        if idx not in seen:
            seen.add(idx)
            out.append(idx)
    return out


def extract_frames(
    video_path: str,
    out_dir: str,
    *,
    max_frames: int = DEFAULT_MAX_FRAMES,
    blur_threshold: float = DEFAULT_BLUR_THRESHOLD,
    sample_cap: int = DEFAULT_SAMPLE_CAP,
    jpeg_quality: int = 92,
) -> FrameExtraction:
    """Write sharp stills with content-addressed clip and source-frame identities.

    Known-length clips are sampled evenly. Unknown-length sources are decoded only
    through ``sample_cap`` plus one lookahead, with explicit truncation accounting.
    Only the best ``max_frames`` decoded images remain in memory. Decode, encode and
    filesystem errors raise; no failed candidate is silently treated as a blur cull.
    """
    for name, value in (("max_frames", max_frames), ("sample_cap", sample_cap)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if (isinstance(jpeg_quality, bool) or not isinstance(jpeg_quality, int)
            or not 1 <= jpeg_quality <= 100):
        raise ValueError("jpeg_quality must be an integer between 1 and 100")
    if not math.isfinite(blur_threshold) or blur_threshold < 0:
        raise ValueError("blur_threshold must be finite and nonnegative")
    cv2 = require_cv2()
    src = Path(video_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"no such video file: {video_path}")
    with src.open("rb") as stream:
        clip_id = hashlib.file_digest(stream, "sha256").hexdigest()
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        cap.release()
        raise FileNotFoundError(f"OpenCV could not open video: {video_path}")
    result = FrameExtraction(
        blur_threshold=blur_threshold, clip_id=clip_id, source_path=str(src),
        sample_cap=sample_cap,
    )
    # Heap ties prefer the earliest source frame; indices are unique, so image
    # arrays are never compared. Memory is bounded independently of clip length.
    best: list[tuple[float, int, Any]] = []

    def score(idx, frame):
        result.sampled += 1
        sharpness = blur_score(frame)
        if not math.isfinite(sharpness):
            raise ValueError(f"invalid sharpness for source frame {idx}")
        if sharpness < blur_threshold:
            result.rejected_blurry += 1
        elif len(best) < max_frames:
            heapq.heappush(best, (sharpness, -idx, frame))
        else:
            result.rejected_limit += 1
            if (sharpness, -idx) > best[0][:2]:
                heapq.heapreplace(best, (sharpness, -idx, frame))

    try:
        reported = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        total = int(reported) if math.isfinite(reported) and reported > 0 else 0
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        fps = fps if math.isfinite(fps) and fps > 0 else 0
        result.total_frames = total
        result.count_known = total > 0
        if total > 0:
            result.sampling_limited = total > sample_cap
            for idx in _even_indices(total, sample_cap):
                if not cap.set(cv2.CAP_PROP_POS_FRAMES, idx):
                    raise OSError(f"could not seek to source frame {idx}")
                ok, frame = cap.read()
                if not ok or frame is None or frame.size == 0:
                    raise OSError(f"could not decode source frame {idx} of {total}")
                result.decoded += 1
                score(idx, frame)
        else:
            for idx in range(sample_cap + 1):
                ok, frame = cap.read()
                if not ok:
                    if idx == 0:
                        raise OSError("video contains no decodable frames")
                    break
                if frame is None or frame.size == 0:
                    raise OSError(f"could not decode source frame {idx}")
                result.decoded += 1
                if idx == sample_cap:
                    result.truncated = True
                    result.sampling_limited = True
                    break
                score(idx, frame)
            result.total_frames = result.decoded
            result.warnings.append(
                f"source frame count unavailable; decoded {result.decoded} frames"
                + (f"; truncated sampling at {sample_cap} candidates"
                   if result.truncated else "; decoder reached end of stream")
                + "; source length and decoder EOF cannot be independently verified"
            )
    finally:
        cap.release()

    if result.sampling_limited and result.count_known:
        result.warnings.append(f"sampled {result.sampled} of {result.total_frames} source frames")
    if result.rejected_limit:
        result.warnings.append(f"discarded {result.rejected_limit} sharp candidates at max_frames={max_frames}")
    out = Path(out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    # Stage all selected JPEGs before publishing: a failed encoder cannot leave
    # empty/partial JPEGs where the live job's image enumeration would see them.
    with tempfile.TemporaryDirectory(prefix=".video-", dir=out) as staging:
        pending = []
        for sharpness, neg_idx, frame in sorted(best, key=lambda row: -row[1]):
            idx = -neg_idx
            filename = f"clip_{clip_id}_frame_{idx:09d}.jpg"
            fpath = out / filename
            if not fpath.is_file() or fpath.stat().st_size == 0:
                temporary = Path(staging) / filename
                if not cv2.imwrite(str(temporary), frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]):
                    raise OSError(f"could not write extracted source frame {idx}")
                if not temporary.is_file() or temporary.stat().st_size == 0:
                    raise OSError(f"encoder produced no data for source frame {idx}")
                pending.append((temporary, fpath))
            result.frames.append(FrameRecord(
                path=str(fpath), frame_index=idx,
                time_s=round(idx / fps, 4) if fps else 0.0,
                sharpness=round(sharpness, 3), source_path=str(src), clip_id=clip_id,
            ))
        for temporary, fpath in pending:
            os.replace(temporary, fpath)
    return result


def extract_frames_safe(video_path: str, out_dir: str, **kw) -> dict[str, Any]:
    """Contract-shaped wrapper: never raises, returns ``{ok, ...}`` per ``reveng.md`` §0.

    On success ``{ok: True, **extraction.to_dict()}``; on any expected failure
    ``{ok: False, reason, hint}``. This is the shape the MCP ``reveng_add_video`` adapter
    forwards — the raising :func:`extract_frames` stays available for typed callers.
    """
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
    out = {"ok": True}
    out.update(res.to_dict())
    return out
