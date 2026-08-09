"""Content-addressed, resumable rendered-pixel corpus generation."""
from __future__ import annotations

 
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Iterable

import numpy as np

from services.viewpoint.accel import evaluate_many
from services.viewpoint.observation import ObservationConfig, observe
from services.viewpoint.oracle import ORACLE_VERSION
from services.viewpoint.parts import GENERATOR_VERSION, PartExample, generate_part, generate_part_examples
from services.viewpoint.sampling import generate_benchmark_candidate_set

from .raster import RASTERIZER_VERSION, decode_png, encode_png, image_digest, rasterize
from .schema import COMPONENTS, PIXEL_SCHEMA_VERSION, PixelCorpus, PixelCorpusConfig, PixelInputs, PixelTargets

_CORPUS_GENERATOR_VERSION = "viewpoint-pixel-corpus/1.4.0"
_FEATURE_KINDS = ("face", "edge", "hole", "boss", "pocket", "region")
_QUESTION_KINDS = ("presence", "dimension", "surface", "clearance", "orientation", "identity")
_FORBIDDEN_INPUT_KEYS = frozenset({
    "components", "labels", "oracle", "occlusion", "ambiguous_feature_ids",
    "feature_id", "target_id", "target_object",
})


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _config_payload(config: PixelCorpusConfig) -> dict[str, object]:
    return {
        "parts": config.parts,
        "part_indices": list(config.part_indices) if config.part_indices is not None else list(range(config.parts)),
        "examples_per_part": config.examples_per_part,
        "candidates": config.candidates,
        "seed": config.seed,
        "resolution": list(config.resolution),
        "oracle_image_size": list(config.oracle_image_size),
        "include_depth": config.include_depth,
        "renderer_version": RASTERIZER_VERSION,
        "oracle_version": ORACLE_VERSION,
        "part_generator_version": GENERATOR_VERSION,
        "corpus_generator_version": _CORPUS_GENERATOR_VERSION,
    }


def _atomic_bytes(path: Path, content: bytes) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".partial-{os.getpid()}")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _atomic_json(path: Path, value: object) -> None:
    content = json.dumps(value, indent=2, sort_keys=False, ensure_ascii=True).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".partial-{os.getpid()}")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _npy_content(array: np.ndarray) -> bytes:
    import io
    stream = io.BytesIO()
    np.save(stream, np.ascontiguousarray(array), allow_pickle=False)
    return stream.getvalue()


def _put_object(root: Path, family: str, suffix: str, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    _atomic_bytes(root / "objects" / family / f"{digest}.{suffix}", content)
    return digest


def _candidate_seed(config: PixelCorpusConfig, part_index: int, example_index: int) -> int:
    sequence = np.random.SeedSequence((config.seed, part_index, example_index, 0x1A11))
    return int(sequence.generate_state(1, dtype=np.uint32)[0])


def _observation_seed(config: PixelCorpusConfig, part_index: int, example_index: int, candidate_index: int) -> int:
    sequence = np.random.SeedSequence((config.seed, part_index, example_index, candidate_index, 0x51A6E))
    return int(sequence.generate_state(1, dtype=np.uint32)[0])



def _task(example: PartExample) -> np.ndarray:
    """Query metadata only: two classes and two fixed scalars, never feature geometry."""
    result = np.zeros(14, dtype=np.float32)
    result[_FEATURE_KINDS.index(example.feature_kind)] = 1.0
    result[6 + _QUESTION_KINDS.index(example.question_kind)] = 1.0
    result[-2:] = (0.25, 0.5)
    return result



def _camera_vector(camera) -> np.ndarray:
    return np.asarray(
        (
            *camera.position,
            *camera.forward,
            *camera.true_up,
            *camera.right,
            np.deg2rad(camera.vertical_fov_deg),
            camera.image_width_px / camera.image_height_px,
            camera.near,
            camera.far,
        ),
        dtype=np.float32,
    )


def _coarse_prior(example: PartExample, camera, *, seed: int) -> np.ndarray:
    # The public observation is the capability boundary.  A 1x1 capture avoids computing an
    # unused dense depth map; FeatureRegion2D itself is identical at every depth resolution.
    observation = observe(
        example.scene,
        example.target_id,
        camera,
        level="depth_only",
        seed=seed,
        config=ObservationConfig(
            depth_height=1,
            depth_width=1,
            sparse_point_count=1,
            feature_region_quantum_px=4.0,
            feature_uncertainty_px=8.0,
            prior_voxel_size=0.08,
        ),
    )
    return observation.feature_region.as_vector().astype(np.float32)


def _split_map(seed: int) -> dict[int, str]:
    order = np.random.default_rng(seed).permutation(72)
    result = {int(index): "train" for index in order[:50]}
    result.update({int(index): "validation" for index in order[50:61]})
    result.update({int(index): "test" for index in order[61:]})
    return result


def locked_part_indices(*, seed: int = 1729) -> dict[str, tuple[int, ...]]:
    split = _split_map(seed)
    return {name: tuple(index for index in range(72) if split[index] == name) for name in ("train", "validation", "test")}


def _greedy_geometric_index(example: PartExample, cameras: list[object]) -> int:
    """Mirror the frozen limited-information greedy using only public observation metadata."""
    lower = example.scene.mesh.vertices.min(axis=0)
    upper = example.scene.mesh.vertices.max(axis=0)
    workspace_center = (lower + upper) * 0.5
    radius = max(float(np.linalg.norm(upper - lower)) * 0.5, 1.0e-6)
    context_direction = np.asarray((1.0, -1.0, 0.85), np.float64)
    context_direction /= np.linalg.norm(context_direction)
    context_forward = -context_direction
    feature_offset = np.zeros(3, np.float64)
    scores: list[float] = []
    for camera in cameras:
        alignment = abs(float(camera.forward @ context_forward))
        if example.feature_kind == "edge":
            alignment = 1.0 - alignment
        target_offset = (camera.target - workspace_center) / radius
        centering = float(np.exp(-np.linalg.norm(target_offset - feature_offset)))
        distance = max(float(np.linalg.norm(camera.target - camera.position)) / radius, 1.0e-6)
        scale_proxy = float(np.exp(-abs(np.log(distance) - np.log(2.2))))
        fov_proxy = float(np.exp(-2.0 * abs(camera.vertical_fov_deg / 180.0 - 45.0 / 180.0)))
        scores.append(0.45 * alignment + 0.30 * centering + 0.15 * scale_proxy + 0.10 * fov_proxy)
    return int(np.argmax(scores))


def _row_payload(
    config: PixelCorpusConfig,
    part_index: int,
    example_index: int,
    example: PartExample,
    *,
    device: str,
    cameras: list[object] | None = None,
) -> tuple[dict[str, Any], np.ndarray, dict[str, int], str]:
    if cameras is None:
        cameras = generate_benchmark_candidate_set(
            example.scene,
            example.target_id,
            config.candidates,
            seed=_candidate_seed(config, part_index, example_index),
            image_size=config.oracle_image_size,
        )
    labels, backend = evaluate_many(
        example.scene,
        tuple((example.target_id, camera) for camera in cameras),
        device=device,
        return_info=True,
    )
    if device == "cuda" and backend.backend != "cuda":
        raise RuntimeError(f"CUDA oracle requested but observed {backend.backend}")
    image_hashes: list[str] = []
    depth_hashes: list[str] = []
    camera_rows: list[list[float]] = []
    prior_rows: list[list[float]] = []
    rendered: list[tuple[bytes, bytes | None]] = []
    for candidate_index, camera in enumerate(cameras):
        frame = rasterize(example.scene.mesh, camera, resolution=config.resolution)
        png = encode_png(frame.rgb)
        depth_content = None
        if config.include_depth:
            packed = np.concatenate((frame.depth.reshape(-1).view(np.uint8), np.packbits(frame.depth_mask.reshape(-1))))
            depth_content = _npy_content(packed)
        rendered.append((png, depth_content))
        image_hashes.append(image_digest(png))
        depth_hashes.append(hashlib.sha256(depth_content).hexdigest() if depth_content is not None else "")
        camera_rows.append(_camera_vector(camera).tolist())
        prior_rows.append(_coarse_prior(example, camera, seed=_observation_seed(config, part_index, example_index, candidate_index)).tolist())
    input_record: dict[str, Any] = {
        "schema": PIXEL_SCHEMA_VERSION + "/input",
        "resolution": list(config.resolution),
        "images": image_hashes,
        "depth": depth_hashes if config.include_depth else None,
        "cameras": camera_rows,
        "coarse_prior": prior_rows,
        "task": _task(example).tolist(),
    }
    target = np.asarray([[label.quality_components()[name] for name in COMPONENTS] for label in labels], np.float32)
    ideal = -np.ones(3, np.float64) / np.sqrt(3.0)
    fixed_isometric = int(np.argmax([float(camera.forward @ ideal) for camera in cameras]))
    baselines = {"fixed_isometric": fixed_isometric, "greedy_geometric": _greedy_geometric_index(example, cameras)}
    input_record["_rendered"] = rendered
    return input_record, target, baselines, backend.backend


def _assert_no_forbidden_input(value: object, path: str = "input") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in _FORBIDDEN_INPUT_KEYS or any(token in lowered for token in ("occlusion", "oracle", "label")):
                raise ValueError(f"forbidden inference field at {path}.{key}")
            _assert_no_forbidden_input(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_no_forbidden_input(child, f"{path}[{index}]")


def _load_manifest(path: Path, corpus_id: str, payload: dict[str, object]) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema": PIXEL_SCHEMA_VERSION,
            "corpus_id": corpus_id,
            "config": payload,
            "seed": payload["seed"],
            "renderer_version": RASTERIZER_VERSION,
            "oracle_version": ORACLE_VERSION,
            "part_generator_version": GENERATOR_VERSION,
            "entries": [],
            "generation_seconds": 0.0,
            "oracle_backends": [],
        }
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("corpus_id") != corpus_id or manifest.get("config") != payload:
        raise ValueError("manifest does not match requested corpus configuration")
    return manifest


def _depth_from_object(path: Path, *, height: int, width: int) -> tuple[np.ndarray, np.ndarray]:
    packed = np.load(path, allow_pickle=False)
    pixel_bytes = height * width * np.dtype(np.float32).itemsize
    raw = np.asarray(packed, np.uint8)
    depth = raw[:pixel_bytes].tobytes()
    depth_array = np.frombuffer(depth, np.float32).copy().reshape(height, width)
    mask = np.unpackbits(raw[pixel_bytes:])[: height * width].astype(bool).reshape(height, width)
    return depth_array, mask


def _materialize(root: Path, manifest: dict[str, Any]) -> PixelCorpus:
    inputs_rgb: list[np.ndarray] = []
    inputs_depth: list[np.ndarray] = []
    inputs_depth_mask: list[np.ndarray] = []
    cameras: list[np.ndarray] = []
    priors: list[np.ndarray] = []
    tasks: list[np.ndarray] = []
    targets: list[np.ndarray] = []
    part_ids: list[str] = []
    difficulties: list[str] = []
    splits: list[str] = []
    fixed: list[int] = []
    greedy: list[int] = []
    width, height = manifest["config"]["resolution"]
    include_depth = bool(manifest["config"]["include_depth"])
    for entry in manifest["entries"]:
        record = json.loads((root / "objects" / "inputs" / f"{entry['input_object']}.json").read_text(encoding="utf-8"))
        _assert_no_forbidden_input(record)
        inputs_rgb.append(np.stack([decode_png((root / "objects" / "images" / f"{digest}.png").read_bytes()) for digest in record["images"]]))
        if include_depth:
            pairs = [_depth_from_object(root / "objects" / "depth" / f"{digest}.npy", height=height, width=width) for digest in record["depth"]]
            inputs_depth.append(np.stack([pair[0] for pair in pairs]))
            inputs_depth_mask.append(np.stack([pair[1] for pair in pairs]))
        cameras.append(np.asarray(record["cameras"], np.float32))
        priors.append(np.asarray(record["coarse_prior"], np.float32))
        tasks.append(np.asarray(record["task"], np.float32))
        targets.append(np.load(root / "objects" / "targets" / f"{entry['target_object']}.npy", allow_pickle=False))
        part_ids.append(entry["part_id"])
        difficulties.append(entry["difficulty"])
        splits.append(entry["split"])
        fixed.append(int(entry["baselines"]["fixed_isometric"]))
        greedy.append(int(entry["baselines"]["greedy_geometric"]))
    count = len(part_ids)
    candidate_count = int(manifest["config"]["candidates"])
    rng = np.random.default_rng(np.random.SeedSequence((int(manifest["seed"]), candidate_count, 0xA11CE)))
    random_indices = rng.integers(candidate_count, size=count, dtype=np.int64)
    pixel_inputs = PixelInputs(
        rgb=np.stack(inputs_rgb),
        depth=np.stack(inputs_depth) if include_depth else None,
        depth_mask=np.stack(inputs_depth_mask) if include_depth else None,
        cameras=np.stack(cameras),
        coarse_prior=np.stack(priors),
        task=np.stack(tasks),
        candidate_mask=np.ones((count, candidate_count), bool),
    )
    pixel_targets = PixelTargets(
        components=np.stack(targets),
        baseline_indices={
            "seeded_random": random_indices,
            "fixed_isometric": np.asarray(fixed, np.int64),
            "greedy_geometric": np.asarray(greedy, np.int64),
        },
    )
    return PixelCorpus(
        pixel_inputs,
        pixel_targets,
        tuple(part_ids),
        tuple(difficulties),
        tuple(splits),
        root / "manifest.json",
        manifest["renderer_version"],
        manifest["oracle_version"],
        float(manifest.get("generation_seconds", 0.0)),
        int(manifest.get("cache_hits", 0)),
    ).validate()


def generate_pixel_corpus(config: PixelCorpusConfig = PixelCorpusConfig(), *, workers: int = 6, device: str = "auto") -> PixelCorpus:
    """Generate or resume the exact corpus, then materialize it for training.

    The CUDA accelerator labels rows in batches.  Rendering stays in-process because a row is
    only sixteen 160x120 views and process transfer would dominate it; ``workers`` is bounded
    now so a future renderer cannot silently violate workstation manners.
    """
    config.validate()
    if not 1 <= int(workers) <= 6:
        raise ValueError("workers must be in [1, 6]")
    if device not in ("auto", "cuda", "cpu", "numpy"):
        raise ValueError("device must be auto, cuda, cpu, or numpy")
    payload = _config_payload(config)
    corpus_id = hashlib.sha256(_canonical_json(payload)).hexdigest()
    root = Path(config.cache_dir)
    manifest_path = root / "manifest.json"
    manifest = _load_manifest(manifest_path, corpus_id, payload)
    existing = {entry["row_id"] for entry in manifest["entries"]}
    cache_hits = len(existing)
    split_map = _split_map(config.seed)
    part_indices = config.part_indices if config.part_indices is not None else tuple(range(config.parts))
    started = time.perf_counter()
    for part_index in part_indices:
        examples = generate_part_examples(part_index, config.examples_per_part, seed=config.seed)
        for example_index, example in enumerate(examples):
            row_id = hashlib.sha256(_canonical_json((corpus_id, int(part_index), example_index))).hexdigest()
            if row_id in existing:
                continue
            input_record, target, baselines, backend = _row_payload(config, int(part_index), example_index, example, device=device)
            rendered = input_record.pop("_rendered")
            for (png, depth_content), image_hash, depth_hash in zip(rendered, input_record["images"], input_record["depth"] or [""] * len(rendered)):
                _atomic_bytes(root / "objects" / "images" / f"{image_hash}.png", png)
                if depth_content is not None:
                    _atomic_bytes(root / "objects" / "depth" / f"{depth_hash}.npy", depth_content)
            _assert_no_forbidden_input(input_record)
            input_content = _canonical_json(input_record)
            input_object = _put_object(root, "inputs", "json", input_content)
            target_object = _put_object(root, "targets", "npy", _npy_content(target))
            split = split_map.get(int(part_index), "train")
            manifest["entries"].append({
                "row_id": row_id,
                "input_object": input_object,
                "target_object": target_object,
                "part_id": example.part_id,
                "part_index": int(part_index),
                "example_index": example_index,
                "split": split,
                "seed": config.seed,
                "candidate_seed": _candidate_seed(config, int(part_index), example_index),
                "difficulty": example.difficulty,
                "baselines": baselines,
            })
            manifest["oracle_backends"] = sorted(set(manifest["oracle_backends"]) | {backend})
            manifest["generation_seconds"] = float(manifest.get("generation_seconds", 0.0)) + (time.perf_counter() - started)
            started = time.perf_counter()
            manifest["cache_hits"] = cache_hits
            _atomic_json(manifest_path, manifest)
    manifest["generation_seconds"] = float(manifest.get("generation_seconds", 0.0)) + (time.perf_counter() - started)
    manifest["cache_hits"] = cache_hits
    split_counts = {name: sum(1 for entry in manifest["entries"] if entry["split"] == name) for name in ("train", "validation", "test")}
    split_parts = {name: sorted({entry["part_id"] for entry in manifest["entries"] if entry["split"] == name}) for name in split_counts}
    manifest["statistics"] = {
        "rows": len(manifest["entries"]),
        "candidate_images": len(manifest["entries"]) * config.candidates,
        "rows_per_split": split_counts,
        "candidate_images_per_split": {name: count * config.candidates for name, count in split_counts.items()},
        "parts_per_split": {name: len(values) for name, values in split_parts.items()},
        "part_ids_per_split": split_parts,
        "resolution": list(config.resolution),
    }
    _atomic_json(manifest_path, manifest)
    return _materialize(root, manifest)


def generate_visibility_audit_corpus(
    *,
    seed: int = 1729,
    device: str = "auto",
    cache_dir: Path = Path("scratch/viewpoint-pixels/visibility-audit"),
) -> PixelCorpus:
    """Render the frozen 8-part x 8-look pooled visibility-information protocol.

    This reproduces ``observation.benchmark.run_benchmark(parts=8, looks_per_part=8,
    seed=1729)``: part indices 0..7, target ``i % feature_count``, candidate seed
    ``seed + 1009*i`` and 160x120 cameras.  Every row is marked test/evaluation-only.
    """
    config = PixelCorpusConfig(
        parts=8,
        examples_per_part=1,
        candidates=8,
        seed=seed,
        resolution=(160, 120),
        oracle_image_size=(160, 120),
        cache_dir=cache_dir,
        include_depth=True,
        part_indices=tuple(range(8)),
    ).validate()
    payload = _config_payload(config)
    payload["protocol"] = "services.viewpoint.observation.benchmark.run_benchmark/pooled-8x8"
    corpus_id = hashlib.sha256(_canonical_json(payload)).hexdigest()
    root = Path(config.cache_dir)
    manifest_path = root / "manifest.json"
    manifest = _load_manifest(manifest_path, corpus_id, payload)
    existing = {entry["row_id"] for entry in manifest["entries"]}
    cache_hits = len(existing)
    started = time.perf_counter()
    for part_index in range(8):
        part = generate_part(part_index, seed=seed)
        target_id = part.target_feature_ids[part_index % len(part.target_feature_ids)]
        feature = part.scene.feature(target_id)
        example = PartExample(
            part_id=part.part_id,
            scene=part.scene,
            target_id=target_id,
            feature_kind=feature.kind,
            question_kind="presence",
            difficulty=str(feature.difficulty),
        )
        row_id = hashlib.sha256(_canonical_json((corpus_id, part_index, 0))).hexdigest()
        if row_id in existing:
            continue
        cameras = generate_benchmark_candidate_set(
            part.scene,
            target_id,
            8,
            seed=seed + 1009 * part_index,
            image_size=(160, 120),
        )
        input_record, target, baselines, backend = _row_payload(
            config,
            part_index,
            0,
            example,
            device=device,
            cameras=cameras,
        )
        rendered = input_record.pop("_rendered")
        for (png, depth_content), image_hash, depth_hash in zip(
            rendered,
            input_record["images"],
            input_record["depth"],
        ):
            _atomic_bytes(root / "objects" / "images" / f"{image_hash}.png", png)
            if depth_content is not None:
                _atomic_bytes(root / "objects" / "depth" / f"{depth_hash}.npy", depth_content)
        _assert_no_forbidden_input(input_record)
        input_object = _put_object(root, "inputs", "json", _canonical_json(input_record))
        target_object = _put_object(root, "targets", "npy", _npy_content(target))
        manifest["entries"].append({
            "row_id": row_id,
            "input_object": input_object,
            "target_object": target_object,
            "part_id": part.part_id,
            "part_index": part_index,
            "example_index": 0,
            "split": "test",
            "seed": seed,
            "candidate_seed": seed + 1009 * part_index,
            "difficulty": str(feature.difficulty),
            "baselines": baselines,
        })
        manifest["oracle_backends"] = sorted(set(manifest["oracle_backends"]) | {backend})
        manifest["generation_seconds"] = float(manifest.get("generation_seconds", 0.0)) + (time.perf_counter() - started)
        started = time.perf_counter()
        manifest["cache_hits"] = cache_hits
        _atomic_json(manifest_path, manifest)
    manifest["generation_seconds"] = float(manifest.get("generation_seconds", 0.0)) + (time.perf_counter() - started)
    manifest["cache_hits"] = cache_hits
    manifest["statistics"] = {
        "rows": 8,
        "candidate_images": 64,
        "rows_per_split": {"train": 0, "validation": 0, "test": 8},
        "candidate_images_per_split": {"train": 0, "validation": 0, "test": 64},
        "parts_per_split": {"train": 0, "validation": 0, "test": 8},
        "part_ids_per_split": {
            "train": [],
            "validation": [],
            "test": [entry["part_id"] for entry in manifest["entries"]],
        },
        "resolution": [160, 120],
        "pooled_visibility_samples": 64,
    }
    _atomic_json(manifest_path, manifest)
    return _materialize(root, manifest)


__all__ = ["generate_pixel_corpus", "generate_visibility_audit_corpus", "locked_part_indices"]
