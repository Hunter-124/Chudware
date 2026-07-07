"""Video → blur-culled stills — the capture front-end for the Reverse Engineering pipeline.

A user hands the RE workspace a phone video that orbits the part; photogrammetry / 3DGS want
a handful of *sharp*, well-spread stills, not every motion-blurred frame. This module extracts
frames on an even time grid, scores each by **focus** (variance of the Laplacian — the classic
Pech-Pacheco no-reference sharpness metric), culls the blurry ones, and writes the survivors
into the session ``frames/`` directory (contract ``reveng.md`` §1 layout).

``cv2`` (Apache-2.0) is optional: this module imports without it and reuses
:func:`~engine.vision.fiducial.require_cv2` / :class:`~engine.vision.fiducial.VisionUnavailable`
so a box without OpenCV degrades to a contract-shaped ``{ok: False, reason, hint}`` instead of a
traceback. No new dependency is introduced — OpenCV is already the vision-module runtime dep.

Design (both knobs are explicit parameters, defaults documented — no silent magic):

* **sample-then-cull.** We first sample up to ``sample_cap`` candidate frames evenly across the
  whole video (so a long clip is not front-loaded), score each, drop everything below
  ``blur_threshold``, then keep the ``max_frames`` sharpest of the survivors. Sampling before
  culling keeps the kept stills spread across the orbit instead of clustered wherever the camera
  happened to hold still.
* **absolute Laplacian-variance threshold.** ``blur_threshold`` is scene-dependent in general;
  the default (:data:`DEFAULT_BLUR_THRESHOLD`) is a sane starting point for phone video and is a
  parameter the caller (or a later adaptive pass) can override. A synthetic fixture controls the
  sharp-vs-blurred separation directly, so the fixed default is exercised deterministically.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .fiducial import HAVE_CV2, VisionUnavailable, require_cv2

#: Below this variance-of-Laplacian a frame is judged too blurry to keep. Tuned for downscaled
#: phone video; override per-session if a scene runs consistently softer/sharper.
DEFAULT_BLUR_THRESHOLD: float = 100.0

#: Contract default (``reveng_add_video(..., max_frames=120)``): most stills we keep.
DEFAULT_MAX_FRAMES: int = 120

#: Most candidate frames we decode+score before culling. Bounds work on a long/high-fps clip;
#: the even sample still spans the whole video.
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "frame_index": self.frame_index,
            "time_s": self.time_s,
            "sharpness": self.sharpness,
        }


@dataclass
class FrameExtraction:
    """Result of :func:`extract_frames`: kept stills + honest cull accounting."""

    frames: list[FrameRecord] = field(default_factory=list)
    sampled: int = 0          # candidate frames decoded + scored
    rejected_blurry: int = 0  # scored candidates dropped below threshold
    total_frames: int = 0     # frame count reported by the source video
    blur_threshold: float = DEFAULT_BLUR_THRESHOLD

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
    """Extract blur-culled stills from ``video_path`` into ``out_dir``.

    Samples up to ``sample_cap`` frames evenly across the whole video, scores each by
    variance-of-Laplacian, drops everything below ``blur_threshold``, then writes the
    ``max_frames`` sharpest survivors (in source-time order) as JPEGs.

    Raises :class:`VisionUnavailable` if OpenCV is not installed and
    :class:`FileNotFoundError` if the video cannot be opened — the MCP adapter turns those
    into a contract-shaped ``{ok: False, reason, hint}`` rather than a traceback.
    """
    cv2 = require_cv2()

    src = Path(video_path).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"no such video file: {video_path}")

    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        cap.release()
        raise FileNotFoundError(f"OpenCV could not open video: {video_path}")

    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)

        # Some containers report 0/garbage frame counts; fall back to a linear read.
        if total > 0:
            want = _even_indices(total, sample_cap)
            candidates = []
            for idx in want:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                candidates.append((idx, frame))
        else:
            candidates = []
            idx = 0
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                candidates.append((idx, frame))
                idx += 1
            total = len(candidates)
            # apply the sample cap post-hoc on an unknown-length stream
            if sample_cap and len(candidates) > sample_cap:
                keep = set(_even_indices(len(candidates), sample_cap))
                candidates = [c for i, c in enumerate(candidates) if i in keep]
    finally:
        cap.release()

    result = FrameExtraction(
        sampled=len(candidates),
        total_frames=total,
        blur_threshold=blur_threshold,
    )

    scored: list[tuple[int, float, Any]] = []
    for idx, frame in candidates:
        s = blur_score(frame)
        if s < blur_threshold:
            result.rejected_blurry += 1
            continue
        scored.append((idx, s, frame))

    # keep the sharpest max_frames, then re-order by source time for a coherent orbit
    scored.sort(key=lambda t: t[1], reverse=True)
    if max_frames and len(scored) > max_frames:
        scored = scored[:max_frames]
    scored.sort(key=lambda t: t[0])

    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    for i, (idx, s, frame) in enumerate(scored):
        time_s = (idx / fps) if fps > 0 else 0.0
        fpath = out / f"frame_{i:04d}.jpg"
        cv2.imwrite(str(fpath), frame, [cv2.IMWRITE_JPEG_QUALITY, int(jpeg_quality)])
        result.frames.append(
            FrameRecord(
                path=str(fpath.resolve()),
                frame_index=idx,
                time_s=round(time_s, 4),
                sharpness=round(s, 3),
            )
        )

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
    out = {"ok": True}
    out.update(res.to_dict())
    return out
