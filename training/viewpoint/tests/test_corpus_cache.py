from dataclasses import replace

import numpy as np
import pytest

from training.viewpoint.corpus_cache import cache_key, corpus_inputs, load_or_build
from training.viewpoint.synthetic import CorpusConfig, generate_oracle_corpus_for_indices


def _assert_batches_identical(left, right):
    assert left.part_ids == right.part_ids
    assert left.difficulties == right.difficulties
    for name in (
        "task",
        "geometry",
        "geometry_mask",
        "feature_mask",
        "candidates",
        "candidate_mask",
        "components",
    ):
        assert np.array_equal(getattr(left, name), getattr(right, name)), name


def test_cache_hit_is_bit_identical_to_fresh_generation(tmp_path):
    config = CorpusConfig(parts=3, examples_per_part=1, candidates=3, seed=23, image_size=(32, 32))
    indices = (0, 1, 2)
    inputs = corpus_inputs("full", config, part_indices=indices)
    builds = 0

    def build():
        nonlocal builds
        builds += 1
        return generate_oracle_corpus_for_indices(config, indices, workers=1)

    cold = load_or_build(inputs, build, cache_dir=tmp_path / "cache")
    hit = load_or_build(inputs, build, cache_dir=tmp_path / "cache")
    fresh = generate_oracle_corpus_for_indices(config, indices, workers=1)

    assert not cold.hit
    assert hit.hit
    assert builds == 1
    _assert_batches_identical(cold.batch, hit.batch)
    _assert_batches_identical(hit.batch, fresh)


def test_every_addressed_generator_input_misses_the_cache(tmp_path):
    config = CorpusConfig(parts=3, examples_per_part=1, candidates=3, seed=23, image_size=(32, 32))
    base = corpus_inputs("full", config, part_indices=(0, 1, 2))
    batch = generate_oracle_corpus_for_indices(config, (0, 1, 2), workers=1)
    cache_dir = tmp_path / "cache"
    initial = load_or_build(base, lambda: batch, cache_dir=cache_dir)
    assert not initial.hit

    changes = (
        {**base, "part_count": 4},
        {**base, "part_indices": (0, 1, 3)},
        {**base, "seed": 24},
        {**base, "sampler": "another-sampler/v1"},
        {**base, "candidate_count": 4},
        {**base, "oracle_version": "another-oracle/v1"},
        {**base, "oracle_schema_version": "another-schema/v1"},
        {**base, "cache_schema_version": "another-cache-schema/v1"},
        {**base, "generator_version": "another-generator/v1"},
        {**base, "generator_config": replace(config, examples_per_part=2)},
        {**base, "generator_config": replace(config, image_size=(64, 32))},
    )
    misses = [load_or_build(changed, lambda: batch, cache_dir=cache_dir) for changed in changes]
    assert all(not result.hit for result in misses)
    assert all(result.key != initial.key for result in misses)
    assert len({result.key for result in misses}) == len(changes)


def test_cuda_corpus_is_bit_identical_to_numpy_reference():
    torch = pytest.importorskip("torch", reason="optional Torch is not installed", exc_type=ImportError)
    if not torch.cuda.is_available():
        pytest.skip("CUDA is unavailable")
    config = CorpusConfig(parts=3, examples_per_part=1, candidates=3, seed=41, image_size=(32, 32))
    indices = (0, 1, 2)
    reference = generate_oracle_corpus_for_indices(config, indices, workers=1, device="cpu")
    accelerated = generate_oracle_corpus_for_indices(config, indices, workers=1, device="cuda")
    _assert_batches_identical(reference, accelerated)
