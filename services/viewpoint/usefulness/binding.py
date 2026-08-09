"""Bind objective questions to rendered corpus candidates and frozen-oracle labels."""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Iterable

import numpy as np

from ..oracle import aggregate_quality
from ..parts import generate_part
from ..pixels.schema import COMPONENTS, PixelCorpus
from .schema import BenchmarkItem, CandidateObservation, CandidateView, QuestionSpec

_MARKER_COLOURS = ((0, 255, 255), (255, 0, 255), (255, 220, 0))


def _stable_unit(*parts: object) -> float:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(sha256(payload).digest()[:8], "big") / float(1 << 64)


def _project_center(center: np.ndarray, camera: np.ndarray, *, width: int, height: int) -> tuple[float, float] | None:
    position = camera[0:3]
    forward = camera[3:6]
    up = camera[6:9]
    right = camera[9:12]
    fov_y = float(camera[12])
    aspect = float(camera[13])
    delta = center - position
    depth = float(delta @ forward)
    if depth <= 1.0e-8:
        return None
    tangent = np.tan(0.5 * fov_y)
    ndc_x = float(delta @ right) / (depth * tangent * aspect)
    ndc_y = float(delta @ up) / (depth * tangent)
    return ((ndc_x + 1.0) * 0.5 * (width - 1), (1.0 - ndc_y) * 0.5 * (height - 1))


def _annotate(image: np.ndarray, camera: np.ndarray, question: QuestionSpec) -> np.ndarray:
    """Draw constant-size identity rings; marker geometry never depends on the answer."""
    result = np.array(image, dtype=np.uint8, copy=True)
    height, width = result.shape[:2]
    seed = int(question.part_id.split("_")[1], 16)
    scene = generate_part(question.part_index, seed=seed).scene
    for marker_index, feature_id in enumerate(question.marker_feature_ids):
        projected = _project_center(scene.feature_center(feature_id), camera, width=width, height=height)
        if projected is None:
            continue
        center_x, center_y = projected
        colour = _MARKER_COLOURS[marker_index % len(_MARKER_COLOURS)]
        radius = max(4, min(width, height) // 16)
        for y in range(max(0, int(center_y) - radius - 1), min(height, int(center_y) + radius + 2)):
            for x in range(max(0, int(center_x) - radius - 1), min(width, int(center_x) + radius + 2)):
                distance = np.hypot(x - center_x, y - center_y)
                if radius - 1.25 <= distance <= radius + 1.25:
                    result[y, x] = colour
        x = int(round(center_x))
        y = int(round(center_y))
        if 0 <= x < width:
            result[max(0, y - 2):min(height, y + 3), x] = colour
        if 0 <= y < height:
            result[y, max(0, x - 2):min(width, x + 3)] = colour
    return result


def _row_lookup(corpus: PixelCorpus) -> dict[tuple[str, int], int]:
    occurrences: dict[str, int] = {}
    result: dict[tuple[str, int], int] = {}
    for row_index, part_id in enumerate(corpus.part_ids):
        example_index = occurrences.get(part_id, 0)
        result[(part_id, example_index)] = row_index
        occurrences[part_id] = example_index + 1
    return result


def bind_pixel_corpus(catalog: Iterable[QuestionSpec], corpus: PixelCorpus) -> tuple[BenchmarkItem, ...]:
    """Attach real rendered rows; each row already carries the frozen seven-component labels."""
    corpus.validate()
    lookup = _row_lookup(corpus)
    items: list[BenchmarkItem] = []
    for question in catalog:
        key = (question.part_id, question.example_index)
        if key not in lookup:
            raise ValueError(f"pixel corpus has no row for {key!r}")
        row_index = lookup[key]
        candidates: list[CandidateView] = []
        for candidate_index in range(corpus.inputs.rgb.shape[1]):
            component_values = corpus.targets.components[row_index, candidate_index]
            components = {name: float(component_values[index]) for index, name in enumerate(COMPONENTS)}
            candidate_id = f"{question.public.question_id}:candidate-{candidate_index:02d}"
            image = _annotate(
                corpus.inputs.rgb[row_index, candidate_index],
                corpus.inputs.cameras[row_index, candidate_index],
                question,
            )
            candidates.append(CandidateView(
                CandidateObservation(candidate_id, image), components, aggregate_quality(components)
            ))
        items.append(BenchmarkItem(question, tuple(candidates)))
    return tuple(items)


def bind_cached_labels_for_controls(
    catalog: Iterable[QuestionSpec], manifest_path: str | Path
) -> tuple[BenchmarkItem, ...]:
    """Load real frozen labels without pixels, exclusively for scripted controls.

    The 1x1 black observation is deliberately not model-capable.  Use
    :func:`bind_pixel_corpus` for any real answerer.
    """
    path = Path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "chudware.viewpoint.pixels/1":
        raise ValueError("manifest is not a viewpoint pixel corpus")
    lookup = {
        (entry["part_id"], int(entry["example_index"])): entry
        for entry in manifest.get("entries", ())
    }
    items: list[BenchmarkItem] = []
    blank = np.zeros((1, 1, 3), dtype=np.uint8)
    for question in catalog:
        key = (question.part_id, question.example_index)
        if key not in lookup:
            raise ValueError(f"pixel manifest has no row for {key!r}")
        entry = lookup[key]
        target_path = path.parent / "objects" / "targets" / f"{entry['target_object']}.npy"
        values = np.load(target_path, allow_pickle=False)
        if values.ndim != 2 or values.shape[1] != len(COMPONENTS):
            raise ValueError(f"invalid target component array for {key!r}")
        views: list[CandidateView] = []
        for candidate_index, component_values in enumerate(values):
            components = {name: float(component_values[index]) for index, name in enumerate(COMPONENTS)}
            candidate_id = f"{question.public.question_id}:candidate-{candidate_index:02d}"
            views.append(CandidateView(
                CandidateObservation(candidate_id, blank), components, aggregate_quality(components)
            ))
        items.append(BenchmarkItem(question, tuple(views)))
    return tuple(items)


def make_instrument_benchmark(
    catalog: Iterable[QuestionSpec], *, candidates: int = 16
) -> tuple[BenchmarkItem, ...]:
    """Hardware-free label lattice for validating analysis and scripted answerers.

    It is not evidence about model usefulness.  Each component is an independent stable
    pseudo-random uniform variate, so a degraded answerer tied to one named component gives
    the measurement code a known relationship to recover.
    """
    if candidates < 2:
        raise ValueError("candidates must be at least two")
    items: list[BenchmarkItem] = []
    for question in catalog:
        views: list[CandidateView] = []
        for candidate_index in range(candidates):
            candidate_id = f"{question.public.question_id}:candidate-{candidate_index:02d}"
            components = {
                name: _stable_unit("component", question.public.question_id, candidate_index, name)
                for name in COMPONENTS
            }
            image = np.zeros((16, 16, 3), dtype=np.uint8)
            image[0, 0] = np.asarray((candidate_index, question.part_index % 256, len(question.public.kind)), np.uint8)
            views.append(CandidateView(
                CandidateObservation(candidate_id, image), components, aggregate_quality(components)
            ))
        items.append(BenchmarkItem(question, tuple(views)))
    return tuple(items)


__all__ = ["bind_cached_labels_for_controls", "bind_pixel_corpus", "make_instrument_benchmark"]
