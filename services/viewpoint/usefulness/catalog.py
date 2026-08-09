"""Deterministic visual-question catalogue derived from procedural part geometry."""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
from typing import Iterable

import numpy as np

from ..geometry import Feature, Scene
from ..parts import generate_part, generate_part_examples
from .schema import PublicQuestion, QuestionSpec

_CIRCULAR_KINDS = frozenset(("hole", "boss"))
_CONCENTRIC_IDS = frozenset(("deep_counterbore", "through_hole_counterbore_end", "through_hole_plain_end"))


def _feature_radius(scene: Scene, feature_id: str) -> float:
    feature = scene.feature(feature_id)
    points = scene.feature_points(feature)
    center = points.mean(axis=0)
    axis = feature.orientation
    if axis is None:
        centered = points - center
    else:
        centered = points - center
        centered = centered - np.outer(centered @ axis, axis)
    radii = np.linalg.norm(centered, axis=1)
    positive = radii[radii > 1.0e-9]
    if len(positive) == 0:
        raise ValueError(f"feature {feature_id!r} has no measurable radial extent")
    return float(np.median(positive))


def _radial_boundary_count(scene: Scene, feature_id: str) -> tuple[int, tuple[float, ...]]:
    """Count distinct circular vertex radii in the marked feature's local face plane."""
    feature = scene.feature(feature_id)
    if feature.orientation is None:
        return 1, (_feature_radius(scene, feature_id),)
    center = scene.feature_center(feature)
    axis = feature.orientation
    reference = _feature_radius(scene, feature_id)
    relative = scene.mesh.vertices - center
    axial = np.abs(relative @ axis)
    planar = relative - np.outer(relative @ axis, axis)
    radii = np.linalg.norm(planar, axis=1)
    selected = radii[(axial <= max(0.22 * reference, 0.04)) & (radii >= 0.45 * reference) & (radii <= 2.8 * reference)]
    values = sorted(float(value) for value in np.concatenate((selected, np.asarray((reference,)))))
    clusters: list[list[float]] = []
    for value in values:
        if not clusters or value - float(np.mean(clusters[-1])) > 0.14 * reference:
            clusters.append([value])
        else:
            clusters[-1].append(value)
    levels = tuple(float(np.mean(cluster)) for cluster in clusters)
    return max(1, len(levels)), levels


def _coaxial_counterpart(scene: Scene, feature_id: str) -> str | None:
    feature = scene.feature(feature_id)
    if feature.orientation is None:
        return None
    center = scene.feature_center(feature)
    radius = _feature_radius(scene, feature_id)
    for other in scene.features:
        if other.feature_id == feature_id or other.kind != "hole" or other.orientation is None:
            continue
        if abs(float(feature.orientation @ other.orientation)) < 0.995:
            continue
        delta = scene.feature_center(other) - center
        residual = delta - feature.orientation * float(delta @ feature.orientation)
        if np.linalg.norm(residual) <= 0.2 * radius and abs(float(delta @ feature.orientation)) > 0.5 * radius:
            return other.feature_id
    return None


def _local_height(scene: Scene, feature_id: str) -> float:
    feature = scene.feature(feature_id)
    if feature.orientation is None:
        raise ValueError(f"feature {feature_id!r} has no height axis")
    center = scene.feature_center(feature)
    radius = _feature_radius(scene, feature_id)
    relative = scene.mesh.vertices - center
    axial = relative @ feature.orientation
    planar = relative - np.outer(axial, feature.orientation)
    local = axial[np.linalg.norm(planar, axis=1) <= 3.2 * radius]
    bounded = local[np.abs(local) <= 8.0 * radius]
    if len(bounded) < 2:
        raise ValueError(f"feature {feature_id!r} has insufficient local mesh support")
    return float(np.max(bounded) - np.min(bounded))


def _concentric(scene: Scene, first_id: str, second_id: str) -> tuple[bool, float]:
    first = scene.feature(first_id)
    second = scene.feature(second_id)
    if first.orientation is None or second.orientation is None:
        return False, float("inf")
    if abs(float(first.orientation @ second.orientation)) < 0.995:
        return False, float("inf")
    delta = scene.feature_center(second) - scene.feature_center(first)
    residual = delta - first.orientation * float(delta @ first.orientation)
    offset = float(np.linalg.norm(residual))
    tolerance = 0.05 * min(_feature_radius(scene, first_id), _feature_radius(scene, second_id))
    return offset <= tolerance, offset


def _question(
    *, kind: str, part_id: str, part_index: int, example_index: int, feature_id: str,
    difficulty: str, prompt: str, choices: tuple[str, ...], correct: str,
    derivation: str, measurements: tuple[tuple[str, float | str], ...],
    markers: tuple[str, ...], labels: tuple[str, ...],
) -> QuestionSpec:
    identity = f"viewpoint-usefulness/v1\x1f{part_id}\x1f{kind}\x1f{example_index}\x1f{feature_id}"
    question_id = "uq_" + sha256(identity.encode("utf-8")).hexdigest()[:24]
    return QuestionSpec(
        public=PublicQuestion(question_id, kind, prompt, choices, labels),
        part_id=part_id,
        part_index=part_index,
        example_index=example_index,
        feature_id=feature_id,
        difficulty=difficulty,
        correct_answer=correct,
        derivation=derivation,
        measurements=measurements,
        marker_feature_ids=markers,
    )


def build_question_catalog(
    part_indices: Iterable[int] = range(522), *, examples_per_part: int = 6, seed: int = 1729
) -> tuple[QuestionSpec, ...]:
    """Build questions only where a real corpus row labels the question's primary feature."""
    if examples_per_part <= 0:
        raise ValueError("examples_per_part must be positive")
    questions: list[QuestionSpec] = []
    for part_index in tuple(int(index) for index in part_indices):
        part = generate_part(part_index, seed=seed)
        examples = generate_part_examples(part_index, examples_per_part, seed=seed)
        selected = {example.target_id: (index, example) for index, example in enumerate(examples)}
        circular = [(index, example) for index, example in enumerate(examples) if example.feature_kind in _CIRCULAR_KINDS]
        holes = [(index, example) for index, example in enumerate(examples) if example.feature_kind == "hole"]

        if holes:
            example_index, example = holes[0]
            count, levels = _radial_boundary_count(part.scene, example.target_id)
            answer = "one" if count == 1 else "two or more"
            questions.append(_question(
                kind="visible_hole_count", part_id=part.part_id, part_index=part_index,
                example_index=example_index, feature_id=example.target_id, difficulty=example.difficulty,
                prompt="How many distinct circular hole boundaries are visible on the face inside the cyan marker?",
                choices=("one", "two or more"), correct=answer,
                derivation="Project mesh vertices into the marked feature plane, cluster local radial levels at 14% of the feature radius, and classify one versus two-or-more boundaries.",
                measurements=(("boundary_count", float(count)), ("radial_levels", ",".join(f"{value:.6g}" for value in levels))),
                markers=(example.target_id,), labels=("cyan target",),
            ))

            through_index, through_example = holes[1] if len(holes) > 1 else holes[0]
            counterpart = _coaxial_counterpart(part.scene, through_example.target_id)
            through = counterpart is not None
            questions.append(_question(
                kind="pocket_throughness", part_id=part.part_id, part_index=part_index,
                example_index=through_index, feature_id=through_example.target_id, difficulty=through_example.difficulty,
                prompt="Is the marked bore/recess through or blind?",
                choices=("through", "blind"), correct="through" if through else "blind",
                derivation="A recess is through exactly when the geometry contains a second hole opening with parallel axis, negligible transverse offset, and nonzero axial separation.",
                measurements=(("opposite_opening", counterpart or "none"),), markers=(through_example.target_id,), labels=("cyan target",),
            ))

        boss_pair = ("inspection_boss", "deep_counterbore")
        primary = next((feature_id for feature_id in boss_pair if feature_id in selected), None)
        if primary is not None:
            example_index, example = selected[primary]
            first_id, second_id = boss_pair
            if int.from_bytes(sha256(part.part_id.encode("utf-8")).digest()[:2], "big") & 1:
                first_id, second_id = second_id, first_id
            first_height = _local_height(part.scene, first_id)
            second_height = _local_height(part.scene, second_id)
            correct = "A" if first_height > second_height else "B"
            questions.append(_question(
                kind="boss_height_comparison", part_id=part.part_id, part_index=part_index,
                example_index=example_index, feature_id=primary, difficulty=example.difficulty,
                prompt="Which marked raised cylindrical feature is taller along its own axis?",
                choices=("A", "B"), correct=correct,
                derivation="For each marked cylinder, restrict mesh vertices to 2.25 local radii and compare the axial max-minus-min extent.",
                measurements=(("A_height", first_height), ("B_height", second_height)), markers=(first_id, second_id), labels=("cyan A", "magenta B"),
            ))

        concentric_primary = next((feature_id for feature_id in selected if feature_id in _CONCENTRIC_IDS), None)
        if concentric_primary is not None:
            example_index, example = selected[concentric_primary]
            if concentric_primary == "through_hole_counterbore_end":
                other_id = "through_hole_plain_end"
            elif concentric_primary == "through_hole_plain_end":
                other_id = "through_hole_counterbore_end"
            else:
                other_id = "angled_face_hole"
            is_concentric, offset = _concentric(part.scene, concentric_primary, other_id)
            questions.append(_question(
                kind="counterbore_concentricity", part_id=part.part_id, part_index=part_index,
                example_index=example_index, feature_id=concentric_primary, difficulty=example.difficulty,
                prompt="Are the cyan and magenta marked circular features concentric (on one common axis)?",
                choices=("yes", "no"), correct="yes" if is_concentric else "no",
                derivation="Require parallel feature axes, then compare transverse center offset with 5% of the smaller measured radius.",
                measurements=(("transverse_offset", offset),), markers=(concentric_primary, other_id), labels=("cyan", "magenta"),
            ))

        if len(circular) >= 2:
            (first_index, first_example), (_, second_example) = circular[:2]
            first_id, second_id = first_example.target_id, second_example.target_id
            if part_index & 1:
                first_id, second_id = second_id, first_id
                first_index, first_example = circular[1][0], circular[1][1]
            first_radius = _feature_radius(part.scene, first_id)
            second_radius = _feature_radius(part.scene, second_id)
            ratio = first_radius / second_radius
            if ratio < 0.75:
                correct = "less than 0.75"
            elif ratio <= 1.33:
                correct = "0.75 to 1.33"
            else:
                correct = "greater than 1.33"
            questions.append(_question(
                kind="diameter_ratio", part_id=part.part_id, part_index=part_index,
                example_index=first_index, feature_id=first_id, difficulty=first_example.difficulty,
                prompt="Which range contains the approximate diameter ratio cyan A / magenta B?",
                choices=("less than 0.75", "0.75 to 1.33", "greater than 1.33"), correct=correct,
                derivation="Fit each marked feature radius from its geometry landmarks; the diameter ratio equals the radius ratio and is assigned to fixed predeclared bins.",
                measurements=(("A_radius", first_radius), ("B_radius", second_radius), ("ratio", ratio)), markers=(first_id, second_id), labels=("cyan A", "magenta B"),
            ))
    return tuple(questions)


def catalogue_counts(catalog: Iterable[QuestionSpec]) -> dict[str, int]:
    counts = Counter(question.public.kind for question in catalog)
    return {kind: counts.get(kind, 0) for kind in (
        "visible_hole_count", "pocket_throughness", "boss_height_comparison",
        "counterbore_concentricity", "diameter_ratio",
    )}


__all__ = ["build_question_catalog", "catalogue_counts"]
