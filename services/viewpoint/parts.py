"""Seeded mechanical-part scenes for viewpoint-policy benchmarks.

The meshes are intentionally analytic and dependency-free.  They are not boolean CAD
solids: each part is a deterministic assembly of opaque triangle-mesh walls, bosses,
ribs, webs, pockets, and tubes.  The target features sit inside that geometry so the
oracle must account for genuine line-of-sight occlusion rather than cuboid face choice.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geometry import DIFFICULTY_LABELS, Feature, Mesh, Scene, unit

GENERATOR_VERSION = "viewpoint-mechanical-parts/1.0.0"
DIFFICULTIES = DIFFICULTY_LABELS
QUESTION_KINDS = ("presence", "dimension", "surface", "clearance", "orientation", "identity")


@dataclass(frozen=True)
class GeneratedPart:
    part_id: str
    part_index: int
    seed: int
    scene: Scene
    target_feature_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.part_index < 0 or not self.part_id or not self.target_feature_ids:
            raise ValueError("generated part needs a non-negative index, id, and targets")
        if len(set(self.target_feature_ids)) != len(self.target_feature_ids):
            raise ValueError("generated target feature ids must be unique")
        for feature_id in self.target_feature_ids:
            feature = self.scene.feature(feature_id)
            if feature.difficulty not in DIFFICULTIES:
                raise ValueError(f"generated feature {feature_id!r} has no difficulty label")


@dataclass(frozen=True)
class PartExample:
    part_id: str
    scene: Scene
    target_id: str
    feature_kind: str
    question_kind: str
    difficulty: str


class _MeshBuilder:
    def __init__(self) -> None:
        self.vertices: list[np.ndarray] = []
        self.triangles: list[tuple[int, int, int]] = []

    def _vertices(self, points: np.ndarray) -> tuple[int, ...]:
        start = len(self.vertices)
        self.vertices.extend(np.asarray(points, dtype=np.float64))
        return tuple(range(start, start + len(points)))

    def quad(self, points: np.ndarray) -> tuple[int, int]:
        indices = self._vertices(np.asarray(points, dtype=np.float64))
        start = len(self.triangles)
        self.triangles.extend(((indices[0], indices[1], indices[2]), (indices[0], indices[2], indices[3])))
        return start, start + 1

    def box(self, center: np.ndarray, size: np.ndarray) -> dict[str, tuple[int, int]]:
        half = np.asarray(size, dtype=np.float64) * 0.5
        center = np.asarray(center, dtype=np.float64)
        corners = np.asarray(
            (
                (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
                (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
            ),
            dtype=np.float64,
        ) * half + center
        vertex = self._vertices(corners)
        faces = {
            "bottom": ((0, 2, 1), (0, 3, 2)),
            "top": ((4, 5, 6), (4, 6, 7)),
            "front": ((0, 1, 5), (0, 5, 4)),
            "back": ((3, 7, 6), (3, 6, 2)),
            "left": ((0, 4, 7), (0, 7, 3)),
            "right": ((1, 2, 6), (1, 6, 5)),
        }
        result: dict[str, tuple[int, int]] = {}
        for name, face in faces.items():
            start = len(self.triangles)
            self.triangles.extend(tuple(tuple(vertex[index] for index in triangle) for triangle in face))
            result[name] = (start, start + 1)
        return result

    def cylinder(
        self,
        center: np.ndarray,
        radius: float,
        height: float,
        axis: np.ndarray,
        *,
        segments: int = 12,
        caps: bool = True,
    ) -> dict[str, tuple[int, ...]]:
        axis = unit(axis, name="cylinder axis")
        helper = np.asarray((1.0, 0.0, 0.0)) if abs(float(axis[0])) < 0.8 else np.asarray((0.0, 1.0, 0.0))
        first = unit(np.cross(axis, helper), name="cylinder basis")
        second = np.cross(axis, first)
        angles = np.linspace(0.0, 2.0 * np.pi, segments, endpoint=False)
        radial = np.cos(angles)[:, None] * first + np.sin(angles)[:, None] * second
        center = np.asarray(center, dtype=np.float64)
        bottom_center = center - axis * height * 0.5
        top_center = center + axis * height * 0.5
        points = np.concatenate((bottom_center + radius * radial, top_center + radius * radial), axis=0)
        vertex = self._vertices(points)
        side_start = len(self.triangles)
        for index in range(segments):
            nxt = (index + 1) % segments
            self.triangles.extend(
                ((vertex[index], vertex[nxt], vertex[segments + nxt]),
                 (vertex[index], vertex[segments + nxt], vertex[segments + index]))
            )
        result: dict[str, tuple[int, ...]] = {"side": tuple(range(side_start, len(self.triangles)))}
        if caps:
            bottom_vertex, top_vertex = self._vertices(np.asarray((bottom_center, top_center)))
            bottom_start = len(self.triangles)
            for index in range(segments):
                nxt = (index + 1) % segments
                self.triangles.append((bottom_vertex, vertex[nxt], vertex[index]))
            result["bottom"] = tuple(range(bottom_start, len(self.triangles)))
            top_start = len(self.triangles)
            for index in range(segments):
                nxt = (index + 1) % segments
                self.triangles.append((top_vertex, vertex[segments + index], vertex[segments + nxt]))
            result["top"] = tuple(range(top_start, len(self.triangles)))
        return result

    def mesh(self, rotation: np.ndarray, translation: np.ndarray) -> Mesh:
        vertices = np.asarray(self.vertices, dtype=np.float64) @ rotation.T + translation
        return Mesh(vertices, np.asarray(self.triangles, dtype=np.int64))


def _ring(center: np.ndarray, radius: float, axis: np.ndarray, count: int = 12, phase: float = 0.0) -> np.ndarray:
    axis = unit(axis, name="ring axis")
    helper = np.asarray((1.0, 0.0, 0.0)) if abs(float(axis[0])) < 0.8 else np.asarray((0.0, 1.0, 0.0))
    first = unit(np.cross(axis, helper), name="ring basis")
    second = np.cross(axis, first)
    angle = phase + np.linspace(0.0, 2.0 * np.pi, count, endpoint=False)
    return np.asarray(center) + radius * (np.cos(angle)[:, None] * first + np.sin(angle)[:, None] * second)


def _rotation(rng: np.random.Generator) -> np.ndarray:
    axis = unit(rng.normal(size=3), name="part rotation axis")
    angle = float(rng.uniform(-np.pi, np.pi))
    cross = np.asarray(
        ((0.0, -axis[2], axis[1]), (axis[2], 0.0, -axis[0]), (-axis[1], axis[0], 0.0)),
        dtype=np.float64,
    )
    return np.eye(3) * np.cos(angle) + (1.0 - np.cos(angle)) * np.outer(axis, axis) + np.sin(angle) * cross


def _feature(
    feature_id: str,
    kind: str,
    difficulty: str,
    *,
    triangles: tuple[int, ...] = (),
    points: np.ndarray | None = None,
    orientation: np.ndarray | None = None,
) -> Feature:
    return Feature(
        feature_id,
        kind,
        triangle_indices=triangles,
        points=np.empty((0, 3), dtype=np.float64) if points is None else points,
        orientation=orientation,
        difficulty=difficulty,
    )


def generate_part(part_index: int, *, seed: int = 1729) -> GeneratedPart:
    """Generate one novel, parameterized mechanical inspection part.

    The same ``part_index`` and ``seed`` always produce the same underlying scene.  Every
    target is a real member of that one scene, so downstream train/test splits can group
    strictly by ``part_id`` rather than accidentally splitting poses or targets.
    """

    if part_index < 0:
        raise ValueError("part_index must be non-negative")
    sequence = np.random.SeedSequence((int(seed), int(part_index), 0xC4D))
    local_seed = int(sequence.generate_state(1, dtype=np.uint64)[0])
    rng = np.random.default_rng(local_seed)
    scale = float(rng.uniform(0.82, 1.22))
    depth_scale = float(rng.uniform(0.78, 1.38))
    jitter = rng.uniform(-0.22, 0.22, size=(8, 2))
    builder = _MeshBuilder()
    features: list[Feature] = []

    # A slotted backbone, side rails, and cross-webs make the disconnected feature
    # stations read as one manufactured bracket while leaving actual sight corridors.
    builder.box(np.asarray((0.0, 0.0, -0.72 * scale)), np.asarray((13.6, 1.0, 0.6)) * scale)
    builder.box(np.asarray((-6.3 * scale, 0.8 * scale, -0.25 * scale)), np.asarray((0.55, 8.8, 1.5)) * scale)
    builder.box(np.asarray((6.3 * scale, -0.8 * scale, -0.25 * scale)), np.asarray((0.55, 8.8, 1.5)) * scale)
    for x in (-3.2, 0.0, 3.2):
        builder.box(np.asarray((x * scale, 0.0, -0.48 * scale)), np.asarray((0.22, 7.6, 0.75)) * scale)

    station = np.asarray(
        ((-4.7, -3.0), (-1.7, -3.0), (1.7, -3.0), (4.7, -3.0),
         (-4.7, 2.5), (-1.6, 2.5), (1.7, 2.5), (4.7, 2.5)),
        dtype=np.float64,
    )
    station = (station + jitter) * scale

    # Easy datum and inspection boss: useful controls that keep difficulty slices honest.
    datum_faces = builder.box(np.asarray((*station[0], 0.05 * scale)), np.asarray((1.9, 1.7, 0.9)) * scale)
    features.append(_feature("datum_face", "face", "easy", triangles=datum_faces["top"], orientation=np.asarray((0.0, 0.0, 1.0))))
    boss = builder.cylinder(np.asarray((*station[0] + np.asarray((0.0, 0.15 * scale)), 0.85 * scale)), 0.38 * scale, 0.8 * scale, np.asarray((0.0, 0.0, 1.0)))
    features.append(_feature("inspection_boss", "boss", "easy", triangles=boss["top"], orientation=np.asarray((0.0, 0.0, 1.0))))

    # A tall offset boss casts a true geometric occlusion over a small hole rim.
    shadow_center = np.asarray((*station[1], -0.31 * scale))
    shadow_radius = float(rng.uniform(0.18, 0.31)) * scale
    features.append(_feature("boss_shadowed_hole", "hole", "medium", points=_ring(shadow_center, shadow_radius, np.asarray((0.0, 0.0, 1.0))), orientation=np.asarray((0.0, 0.0, 1.0))))
    boss_offset = np.asarray((rng.choice((-1.0, 1.0)) * 0.62 * scale, 0.0, 0.34 * scale))
    builder.cylinder(shadow_center + boss_offset, float(rng.uniform(0.42, 0.58)) * scale, 1.35 * scale, np.asarray((0.0, 0.0, 1.0)))
    builder.box(shadow_center + np.asarray((0.0, 0.0, -0.22 * scale)), np.asarray((1.8, 1.65, 0.32)) * scale)

    # Counterbore: the target is below a tall opaque tube and visible only near its axis.
    counter_depth = float(rng.uniform(1.05, 1.55)) * scale * depth_scale
    counter_center = np.asarray((*station[2], -0.32 * scale))
    counter_axis = np.asarray((0.0, 0.0, 1.0))
    builder.cylinder(counter_center + counter_axis * counter_depth * 0.5, float(rng.uniform(0.52, 0.7)) * scale, counter_depth, counter_axis, caps=False)
    builder.box(counter_center - counter_axis * 0.14 * scale, np.asarray((1.5, 1.5, 0.22)) * scale)
    features.append(_feature("deep_counterbore", "hole", "hard", points=_ring(counter_center, float(rng.uniform(0.25, 0.36)) * scale, counter_axis), orientation=counter_axis))

    # Deep rectangular pocket with four independent walls and a target floor surface.
    pocket_depth = float(rng.uniform(1.1, 1.75)) * scale * depth_scale
    px, py = station[3]
    pocket_size = np.asarray((rng.uniform(0.85, 1.25), rng.uniform(0.7, 1.05))) * scale
    wall = 0.18 * scale
    floor_z = -0.30 * scale
    builder.box(np.asarray((px - pocket_size[0] * 0.5 - wall * 0.5, py, floor_z + pocket_depth * 0.5)), np.asarray((wall, pocket_size[1] + 2 * wall, pocket_depth)))
    builder.box(np.asarray((px + pocket_size[0] * 0.5 + wall * 0.5, py, floor_z + pocket_depth * 0.5)), np.asarray((wall, pocket_size[1] + 2 * wall, pocket_depth)))
    builder.box(np.asarray((px, py - pocket_size[1] * 0.5 - wall * 0.5, floor_z + pocket_depth * 0.5)), np.asarray((pocket_size[0], wall, pocket_depth)))
    builder.box(np.asarray((px, py + pocket_size[1] * 0.5 + wall * 0.5, floor_z + pocket_depth * 0.5)), np.asarray((pocket_size[0], wall, pocket_depth)))
    floor_points = np.asarray(((px - pocket_size[0] / 2, py - pocket_size[1] / 2, floor_z), (px + pocket_size[0] / 2, py - pocket_size[1] / 2, floor_z), (px + pocket_size[0] / 2, py + pocket_size[1] / 2, floor_z), (px - pocket_size[0] / 2, py + pocket_size[1] / 2, floor_z)))
    floor_triangles = builder.quad(floor_points)
    builder.box(np.asarray((px, py, floor_z - 0.12 * scale)), np.asarray((pocket_size[0], pocket_size[1], 0.20 * scale)))
    features.append(_feature("deep_pocket_floor", "pocket", "hard", triangles=floor_triangles, orientation=np.asarray((0.0, 0.0, 1.0))))

    # Angled-face hole: a tilted tube and a nearby web make the correct axis non-global.
    angled_center = np.asarray((*station[4], 0.05 * scale))
    angled_axis = unit(np.asarray((rng.uniform(0.45, 0.75), rng.uniform(-0.28, 0.28), 1.0)), name="angled-hole axis")
    angled_depth = float(rng.uniform(0.9, 1.35)) * scale
    builder.cylinder(angled_center + angled_axis * angled_depth * 0.5, float(rng.uniform(0.42, 0.58)) * scale, angled_depth, angled_axis, caps=False)
    builder.box(angled_center + np.asarray((0.7 * scale, 0.0, 0.45 * scale)), np.asarray((0.22, 1.8, 1.45)) * scale)
    features.append(_feature("angled_face_hole", "hole", "medium", points=_ring(angled_center, 0.27 * scale, angled_axis, phase=float(rng.uniform(0.0, np.pi))), orientation=angled_axis))

    # Concave rib/web corner.  The target edge is visible only through one octant.
    corner = np.asarray((*station[5], -0.25 * scale))
    builder.box(corner + np.asarray((-0.55 * scale, 0.0, 0.65 * scale)), np.asarray((0.2, 1.8, 1.5)) * scale)
    builder.box(corner + np.asarray((0.0, -0.55 * scale, 0.65 * scale)), np.asarray((1.3, 0.2, 1.5)) * scale)
    builder.box(corner + np.asarray((0.0, 0.0, -0.12 * scale)), np.asarray((1.3, 1.3, 0.22)) * scale)
    corner_edge = np.asarray((corner + (0.03, 0.03, 0.02), corner + (0.03, 0.03, 0.85 * scale)))
    features.append(_feature("concave_rib_edge", "edge", "medium", points=corner_edge, orientation=np.asarray((0.0, 0.0, 1.0))))

    # One through-hole, two semantically different ends: counterbored entry and plain exit.
    through_center = np.asarray((*station[6], 0.35 * scale))
    through_axis = unit(np.asarray((1.0, rng.uniform(-0.18, 0.18), rng.uniform(-0.12, 0.12))), name="through-hole axis")
    through_length = float(rng.uniform(1.25, 1.75)) * scale
    builder.cylinder(through_center, 0.43 * scale, through_length, through_axis, caps=False)
    entry_center = through_center + through_axis * through_length * 0.5
    exit_center = through_center - through_axis * through_length * 0.5
    builder.cylinder(entry_center - through_axis * 0.12 * scale, 0.68 * scale, 0.24 * scale, through_axis, caps=False)
    features.append(_feature("through_hole_counterbore_end", "hole", "easy", points=_ring(entry_center, 0.5 * scale, through_axis, count=16), orientation=through_axis))
    features.append(_feature("through_hole_plain_end", "hole", "medium", points=_ring(exit_center, 0.3 * scale, through_axis, count=10), orientation=through_axis))

    # Underside feature beneath a cantilevered overhang, invisible from conventional top views.
    overhang = np.asarray((*station[7], 0.95 * scale))
    builder.box(overhang, np.asarray((2.0, 1.7, 0.35)) * scale)
    builder.box(overhang + np.asarray((-0.82 * scale, 0.0, -0.65 * scale)), np.asarray((0.28, 1.7, 1.45)) * scale)
    underside_center = overhang + np.asarray((0.32 * scale, 0.0, -0.2 * scale))
    features.append(_feature("underside_overhang_hole", "hole", "hard", points=_ring(underside_center, 0.24 * scale, np.asarray((0.0, 0.0, -1.0))), orientation=np.asarray((0.0, 0.0, -1.0))))

    # Translation-invariant twins exercise uniqueness.  A web shadows only the left twin,
    # so some poses resolve the identity while others remain genuinely ambiguous.
    twin_y = float(rng.uniform(-0.45, 0.45)) * scale
    twin_z = 1.45 * scale
    twin_axis = np.asarray((0.0, 1.0, 0.0))
    twin_left_center = np.asarray((-0.58 * scale, twin_y, twin_z))
    twin_right_center = np.asarray((0.58 * scale, twin_y, twin_z))
    for center in (twin_left_center, twin_right_center):
        builder.cylinder(center, 0.31 * scale, 0.75 * scale, twin_axis, caps=False)
    builder.box(twin_left_center + np.asarray((-0.42 * scale, 0.0, 0.0)), np.asarray((0.3, 1.4, 1.25)) * scale)
    features.append(_feature("ambiguous_twin_left", "hole", "hard", points=_ring(twin_left_center + twin_axis * 0.38 * scale, 0.22 * scale, twin_axis), orientation=twin_axis))
    features.append(_feature("ambiguous_twin_right", "hole", "hard", points=_ring(twin_right_center + twin_axis * 0.38 * scale, 0.22 * scale, twin_axis), orientation=twin_axis))

    rotation = _rotation(rng)
    translation = rng.uniform(-0.45, 0.45, size=3) * scale
    transformed: list[Feature] = []
    for feature in features:
        points = feature.points @ rotation.T + translation if len(feature.points) else feature.points
        orientation = None if feature.orientation is None else feature.orientation @ rotation.T
        transformed.append(
            Feature(
                feature.feature_id,
                feature.kind,
                triangle_indices=feature.triangle_indices,
                points=points,
                orientation=orientation,
                difficulty=feature.difficulty,
            )
        )
    scene = Scene(builder.mesh(rotation, translation), tuple(transformed))
    target_ids = tuple(feature.feature_id for feature in transformed)
    part_id = f"mechanical_{int(seed) & 0xFFFFFFFF:08x}_{part_index:04d}"
    return GeneratedPart(part_id, part_index, int(seed), scene, target_ids)


def generate_part_corpus(count: int = 24, *, seed: int = 1729) -> tuple[GeneratedPart, ...]:
    if count <= 0:
        raise ValueError("count must be positive")
    return tuple(generate_part(index, seed=seed) for index in range(count))


def generate_part_examples(part_index: int, count: int, *, seed: int = 1729) -> tuple[PartExample, ...]:
    """Select deterministic targets from one shared part scene.

    Selection cycles through a part-specific permutation before repeating, so asking for
    more examples never regenerates geometry and never creates a pose-level split leak.
    """

    if count <= 0:
        raise ValueError("count must be positive")
    part = generate_part(part_index, seed=seed)
    rng = np.random.default_rng(np.random.SeedSequence((int(seed), int(part_index), 0xE7A)))
    order = rng.permutation(len(part.target_feature_ids))
    examples: list[PartExample] = []
    for index in range(count):
        target_id = part.target_feature_ids[int(order[index % len(order)])]
        feature = part.scene.feature(target_id)
        examples.append(
            PartExample(
                part.part_id,
                part.scene,
                target_id,
                feature.kind,
                QUESTION_KINDS[(part_index + 2 * index) % len(QUESTION_KINDS)],
                feature.difficulty or "medium",
            )
        )
    return tuple(examples)
