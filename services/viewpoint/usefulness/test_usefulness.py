from __future__ import annotations
import json

from pathlib import Path

import numpy as np
import pytest

from ..pixels.schema import COMPONENTS, PixelCorpus, PixelInputs, PixelTargets
from ..parts import generate_part_examples
from .analysis import evaluate_answerer, highest_aggregate_selections
from .answerers import BlindAnswerer, ComponentDegradedAnswerer, OracleAnswerer
from .binding import bind_cached_labels_for_controls, bind_pixel_corpus, make_instrument_benchmark
from .catalog import build_question_catalog, catalogue_counts


@pytest.fixture(scope="module")
def catalog():
    return build_question_catalog(range(72))


@pytest.fixture(scope="module")
def instrument_items(catalog):
    return make_instrument_benchmark(catalog, candidates=16)


def test_catalogue_covers_all_question_types_with_geometry_ground_truth(catalog) -> None:
    counts = catalogue_counts(catalog)
    assert counts == {
        "visible_hole_count": 72,
        "pocket_throughness": 72,
        "boss_height_comparison": 51,
        "counterbore_concentricity": 69,
        "diameter_ratio": 72,
    }
    answers_by_kind: dict[str, set[str]] = {}
    corpus_targets = {
        (part_index, example_index): example.target_id
        for part_index in range(72)
        for example_index, example in enumerate(generate_part_examples(part_index, 6))
    }
    for question in catalog:
        assert question.correct_answer in question.public.choices
        assert question.feature_id == corpus_targets[(question.part_index, question.example_index)]
        assert question.part_id not in question.public.question_id
        assert question.feature_id not in question.public.question_id
        assert question.derivation
        assert question.measurements
        assert not hasattr(question.public, "correct_answer")
        assert not hasattr(question.public, "derivation")
        answers_by_kind.setdefault(question.public.kind, set()).add(question.correct_answer)
    assert all(len(answers) >= 2 for answers in answers_by_kind.values())
    assert answers_by_kind["diameter_ratio"] == {"less than 0.75", "0.75 to 1.33", "greater than 1.33"}


def test_public_answerer_boundary_contains_pixels_but_no_labels(instrument_items) -> None:
    item = instrument_items[0]
    observation = item.candidates[0].observation
    assert observation.image.dtype == np.uint8
    assert not hasattr(observation, "oracle_components")
    assert not hasattr(observation, "aggregate")
    assert not hasattr(item.question.public, "measurements")


def test_oracle_is_perfect_and_blind_is_at_declared_chance(instrument_items) -> None:
    oracle = evaluate_answerer(instrument_items, OracleAnswerer.from_items(instrument_items))
    blind = evaluate_answerer(instrument_items, BlindAnswerer(seed=1729))
    assert oracle.accuracy == 1.0
    assert all(trial.correct for trial in oracle.trials)
    assert abs(blind.accuracy - blind.chance_accuracy) < 0.02
    assert abs(blind.by_signal["visibility"].pearson_correlation) < 0.05


def test_degraded_answerer_recovers_named_component_relationship(instrument_items) -> None:
    answerer = ComponentDegradedAnswerer.from_items(instrument_items, component="visibility", seed=1729)
    report = evaluate_answerer(
        instrument_items,
        answerer,
        selections=highest_aggregate_selections(instrument_items),
    )
    visibility = report.by_signal["visibility"]
    assert visibility.pearson_correlation > 0.33
    assert visibility.mutual_information_bits > 0.09
    assert visibility.bin_accuracy[-1][3] - visibility.bin_accuracy[0][3] > 0.45
    assert max(abs(report.by_signal[name].pearson_correlation) for name in COMPONENTS if name != "visibility") < 0.04
    assert report.chosen_view_accuracy is not None
    assert set(report.by_difficulty) == {"easy", "medium", "hard"}
    assert sum(count for count, _ in report.by_difficulty.values()) == len(report.trials)


def test_pixel_binding_preserves_frozen_components_and_adds_answer_neutral_markers(catalog) -> None:
    question = next(item for item in catalog if item.part_index == 0 and item.example_index == 4)
    rows, candidates, height, width = 5, 2, 16, 16
    cameras = np.zeros((rows, candidates, 16), dtype=np.float32)
    cameras[..., 2] = 30.0
    cameras[..., 5] = -1.0
    cameras[..., 7] = 1.0
    cameras[..., 9] = 1.0
    cameras[..., 12] = np.deg2rad(45.0)
    cameras[..., 13] = 1.0
    cameras[..., 14] = 0.1
    cameras[..., 15] = 100.0
    component_values = np.linspace(0.1, 0.9, len(COMPONENTS), dtype=np.float32)
    components = np.broadcast_to(component_values, (rows, candidates, len(COMPONENTS))).copy()
    corpus = PixelCorpus(
        PixelInputs(
            rgb=np.zeros((rows, candidates, height, width, 3), dtype=np.uint8),
            depth=None,
            depth_mask=None,
            cameras=cameras,
            coarse_prior=np.zeros((rows, candidates, 8), dtype=np.float32),
            task=np.zeros((rows, 14), dtype=np.float32),
            candidate_mask=np.ones((rows, candidates), dtype=bool),
        ),
        PixelTargets(components),
        part_ids=(question.part_id,) * rows,
        difficulties=("easy",) * rows,
        splits=("train",) * rows,
        manifest_path=Path("scratch/test-usefulness-manifest.json"),
        renderer_version="test",
        oracle_version="viewpoint-geometric-oracle/1.0.0",
        generation_seconds=0.0,
    ).validate()
    item = bind_pixel_corpus((question,), corpus)[0]
    assert len(item.candidates) == candidates
    assert tuple(item.candidates[0].oracle_components.values()) == pytest.approx(component_values)
    assert np.any(item.candidates[0].observation.image)

def test_cached_label_control_binding_uses_real_manifest_targets(catalog, tmp_path: Path) -> None:
    question = catalog[0]
    target_dir = tmp_path / "objects" / "targets"
    target_dir.mkdir(parents=True)
    values = np.stack((
        np.linspace(0.1, 0.7, len(COMPONENTS), dtype=np.float32),
        np.linspace(0.2, 0.8, len(COMPONENTS), dtype=np.float32),
    ))
    np.save(target_dir / "target.npy", values, allow_pickle=False)
    manifest = {
        "schema": "chudware.viewpoint.pixels/1",
        "entries": [{
            "part_id": question.part_id,
            "example_index": question.example_index,
            "target_object": "target",
        }],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    item = bind_cached_labels_for_controls((question,), manifest_path)[0]
    assert len(item.candidates) == 2
    assert tuple(item.candidates[1].oracle_components.values()) == pytest.approx(values[1])
    assert item.candidates[0].observation.image.shape == (1, 1, 3)



def test_invalid_component_name_is_rejected(instrument_items) -> None:
    with pytest.raises(ValueError, match="unknown inspectability component"):
        ComponentDegradedAnswerer.from_items(instrument_items, component="not-a-component")
