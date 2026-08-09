from __future__ import annotations

from dataclasses import fields
import hashlib
import importlib
import json
from pathlib import Path

import numpy as np
import pytest

from services.viewpoint.pixels import (
    PixelCorpusConfig,
    PixelInputs,
    compare_with_engine,
    generate_pixel_corpus,
    generate_visibility_audit_corpus,
    locked_part_indices,
)


def _small_config(path: Path) -> PixelCorpusConfig:
    return PixelCorpusConfig(
        parts=3,
        examples_per_part=1,
        candidates=2,
        seed=73,
        resolution=(96, 96),
        cache_dir=path,
    )


def test_inference_sample_has_no_oracle_or_exact_occlusion_capability(tmp_path: Path):
    corpus = generate_pixel_corpus(_small_config(tmp_path / "pixels"), workers=1, device="numpy")
    sample = corpus.model_inputs()
    assert isinstance(sample, PixelInputs)
    assert {item.name for item in fields(sample)} == {
        "rgb", "depth", "depth_mask", "cameras", "coarse_prior", "task", "candidate_mask"
    }
    serialized_keys = set(sample.__slots__)
    assert not serialized_keys & {"components", "labels", "oracle", "occlusion", "feature_id", "target_id"}
    # Task is row-level query metadata, not a candidate sensor channel.  It can name only
    # feature/question classes plus fixed min-scale/detail constants: no position, axis,
    # normal, extent, or other exact feature geometry has a slot.
    assert sample.task.shape == (3, 14)
    assert np.all(np.isin(sample.task[:, :12], (0.0, 1.0)))
    np.testing.assert_array_equal(sample.task[:, :6].sum(axis=1), np.ones(3))
    np.testing.assert_array_equal(sample.task[:, 6:12].sum(axis=1), np.ones(3))
    np.testing.assert_array_equal(sample.task[:, 12:], np.tile((0.25, 0.5), (3, 1)))
    assert corpus.targets.components.shape == (3, 2, 7)

    manifest = json.loads(corpus.manifest_path.read_text(encoding="utf-8"))
    first = manifest["entries"][0]
    record = json.loads(
        (corpus.manifest_path.parent / "objects" / "inputs" / f"{first['input_object']}.json").read_text(encoding="utf-8")
    )
    text = json.dumps(record, sort_keys=True).lower()
    for forbidden in ("component", "label", "oracle", "occlusion", "feature_id", "target_id", "target_object"):
        assert forbidden not in text
    assert "target_object" in first  # pairing exists only in the training manifest, never the input object


def test_content_addressed_generation_resumes_and_keeps_parts_unmixed(tmp_path: Path):
    config = _small_config(tmp_path / "pixels")
    first = generate_pixel_corpus(config, workers=1, device="numpy")
    second = generate_pixel_corpus(config, workers=1, device="numpy")
    np.testing.assert_array_equal(first.inputs.rgb, second.inputs.rgb)
    np.testing.assert_array_equal(first.targets.components, second.targets.components)
    assert second.cache_hits == 3
    assert set(first.part_ids[i] for i, split in enumerate(first.splits) if split == "train").isdisjoint(
        first.part_ids[i] for i, split in enumerate(first.splits) if split == "validation"
    )
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["config"]["corpus_generator_version"] == "viewpoint-pixel-corpus/1.5.0"
    assert manifest["corpus_generator_version"] == "viewpoint-pixel-corpus/1.5.0"
    assert manifest["entries"][0]["candidate_seed"] == int(
        np.random.SeedSequence((73, 0, 0, 0x1A11)).generate_state(1, dtype=np.uint32)[0]
    )
    for entry in manifest["entries"]:
        input_path = config.cache_dir / "objects" / "inputs" / f"{entry['input_object']}.json"
        target_path = config.cache_dir / "objects" / "targets" / f"{entry['target_object']}.npy"
        assert hashlib.sha256(input_path.read_bytes()).hexdigest() == entry["input_object"]
        assert hashlib.sha256(target_path.read_bytes()).hexdigest() == entry["target_object"]
        record = json.loads(input_path.read_text(encoding="utf-8"))
        for image in record["images"]:
            image_path = config.cache_dir / "objects" / "images" / f"{image}.png"
            assert hashlib.sha256(image_path.read_bytes()).hexdigest() == image


def test_torch_unimportable_falls_back_without_changing_pixel_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    real_import = importlib.import_module

    def without_torch(name: str, package: str | None = None):
        if name == "torch":
            raise ImportError("deliberately unavailable")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", without_torch)
    corpus = generate_pixel_corpus(_small_config(tmp_path / "no-torch"), workers=1, device="auto")
    manifest = json.loads(corpus.manifest_path.read_text(encoding="utf-8"))
    assert manifest["oracle_backends"] == ["numpy"]
    corpus.model_inputs().validate()


def test_locked_split_is_exactly_50_11_11_and_disjoint():
    split = locked_part_indices(seed=1729)
    assert {name: len(values) for name, values in split.items()} == {"train": 50, "validation": 11, "test": 11}
    assert set(split["train"]).isdisjoint(split["validation"])
    assert set(split["train"]).isdisjoint(split["test"])
    assert set(split["validation"]).isdisjoint(split["test"])
    assert set().union(*map(set, split.values())) == set(range(72))


def test_visibility_audit_reproduces_frozen_8_by_8_protocol(tmp_path: Path):
    corpus = generate_visibility_audit_corpus(seed=1729, device="numpy", cache_dir=tmp_path / "audit")
    assert corpus.inputs.rgb.shape == (8, 8, 120, 160, 3)
    assert corpus.targets.components.shape == (8, 8, 7)
    assert set(corpus.splits) == {"test"}
    assert len(set(corpus.part_ids)) == 8
    manifest = json.loads(corpus.manifest_path.read_text(encoding="utf-8"))
    assert manifest["config"]["protocol"] == "services.viewpoint.observation.benchmark.run_benchmark/pooled-8x8"
    assert manifest["statistics"]["pooled_visibility_samples"] == 64


def test_fast_rasterizer_matches_real_engine_pixels(tmp_path: Path):
    library = Path("core/build-linux/bin/libchudware.so")
    if not library.exists():
        pytest.skip("current checkout has no built engine library")
    result = compare_with_engine(library=library, output_dir=tmp_path / "agreement")
    assert result.union_foreground_pixels > 100
    assert result.foreground_iou >= 0.99
    assert result.rgb_mae_union <= 1.0
