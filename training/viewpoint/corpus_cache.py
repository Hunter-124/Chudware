"""Content-addressed storage for deterministic viewpoint corpora."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Callable, Mapping

import numpy as np

from services.viewpoint.dataset import SCHEMA_VERSION as ORACLE_SCHEMA_VERSION
from services.viewpoint.oracle import ORACLE_VERSION
from services.viewpoint.parts import GENERATOR_VERSION

from .data import ViewpointBatch
from .runtime import DEFAULT_CACHE_DIR, SCRATCH_ROOT

CACHE_SCHEMA_VERSION = "training.viewpoint.corpus-cache/v1"
SAMPLER_VERSION = "services.viewpoint.sampling.generate_benchmark_candidate_set/v1"


def _canonical(value: Any) -> Any:
    if is_dataclass(value):
        return _canonical(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        return value.item()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"cache inputs must be JSON-like or dataclasses, got {type(value).__name__}")


def corpus_inputs(
    kind: str,
    config: Any,
    *,
    part_indices: tuple[int, ...],
) -> dict[str, Any]:
    """Describe every input that can alter a generated corpus."""
    return {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "oracle_schema_version": ORACLE_SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "oracle_version": ORACLE_VERSION,
        "sampler": SAMPLER_VERSION,
        "kind": kind,
        "part_count": len(part_indices),
        "part_indices": part_indices,
        "seed": int(config.seed),
        "candidate_count": int(config.candidates),
        "generator_config": config,
    }


def cache_key(inputs: Mapping[str, Any]) -> str:
    encoded = json.dumps(_canonical(inputs), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _metadata(inputs: Mapping[str, Any], key: str) -> str:
    return json.dumps(
        {"key": key, "inputs": _canonical(inputs)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _save(path: Path, batch: ViewpointBatch, metadata: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
    try:
        with temporary.open("wb") as stream:
            np.savez(
                stream,
                cache_metadata=np.asarray(metadata),
                part_ids=np.asarray(batch.part_ids),
                task=batch.task,
                geometry=batch.geometry,
                geometry_mask=batch.geometry_mask,
                feature_mask=batch.feature_mask,
                candidates=batch.candidates,
                candidate_mask=batch.candidate_mask,
                components=batch.components,
                difficulties=np.asarray(batch.difficulties),
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load(path: Path, metadata: str) -> ViewpointBatch:
    with np.load(path, allow_pickle=False) as data:
        if str(data["cache_metadata"].item()) != metadata:
            raise ValueError("cache metadata does not match its content address")
        return ViewpointBatch(
            part_ids=tuple(str(value) for value in data["part_ids"]),
            task=data["task"],
            geometry=data["geometry"],
            geometry_mask=data["geometry_mask"],
            feature_mask=data["feature_mask"],
            candidates=data["candidates"],
            candidate_mask=data["candidate_mask"],
            components=data["components"],
            difficulties=tuple(str(value) for value in data["difficulties"]),
        ).validate()


@dataclass(frozen=True)
class CachedCorpus:
    batch: ViewpointBatch
    hit: bool
    key: str
    path: Path


def load_or_build(
    inputs: Mapping[str, Any],
    build: Callable[[], ViewpointBatch],
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> CachedCorpus:
    """Load the exact addressed corpus or atomically publish a fresh one."""
    normalized = _canonical(inputs)
    key = cache_key(normalized)
    namespace = str(normalized.get("kind", "corpus")).replace("/", "-")
    path = Path(cache_dir) / f"{namespace}-{key}.npz"
    metadata = _metadata(normalized, key)
    if path.exists():
        try:
            return CachedCorpus(_load(path, metadata), True, key, path)
        except (OSError, KeyError, ValueError):
            # A torn/corrupt artifact is never accepted as a hit. Atomic rewrite below repairs it.
            pass
    batch = build().validate()
    _save(path, batch, metadata)
    return CachedCorpus(batch, False, key, path)


def invalidate(cache_dir: Path = DEFAULT_CACHE_DIR) -> None:
    """Delete only the disposable viewpoint content cache."""
    root = Path(cache_dir).resolve()
    scratch = SCRATCH_ROOT.resolve()
    try:
        relative = root.relative_to(scratch)
    except ValueError as exc:
        raise ValueError("refusing to invalidate a cache outside the repository scratch directory") from exc
    if not relative.parts:
        raise ValueError("refusing to invalidate the repository scratch root")
    shutil.rmtree(root, ignore_errors=True)


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "CachedCorpus",
    "SAMPLER_VERSION",
    "cache_key",
    "corpus_inputs",
    "invalidate",
    "load_or_build",
]
