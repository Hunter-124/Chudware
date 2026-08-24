from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from training.simulation.registry import (
    SIMULATION_FAMILIES,
    RegistryValidationError,
    load_registry,
    refresh_baseline_source_hashes,
    validate_registry,
    verify_baseline_source_hashes,
)


REGISTRY_PATH = Path(__file__).with_name("registry.json")
REPO_ROOT = Path(__file__).resolve().parents[2]


class SimulationOptimizerRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with REGISTRY_PATH.open(encoding="utf-8") as stream:
            cls.raw = json.load(stream)

    def invalid(self, mutate) -> RegistryValidationError:
        data = copy.deepcopy(self.raw)
        mutate(data)
        with self.assertRaises(RegistryValidationError) as caught:
            validate_registry(data)
        return caught.exception

    def test_shipped_registry_is_valid_and_complete(self) -> None:
        data = load_registry(REGISTRY_PATH)
        self.assertEqual(
            {track["simulation_family"] for track in data["tracks"]},
            SIMULATION_FAMILIES,
        )
        self.assertEqual(len(data["tracks"]), len(SIMULATION_FAMILIES))

    def test_only_polymesh_is_a_trained_candidate(self) -> None:
        trained = [
            track for track in self.raw["tracks"] if track["model"]["status"] == "trained"
        ]
        self.assertEqual([track["track_id"] for track in trained], ["polymesh_mesh_advisor"])
        candidate = trained[0]
        self.assertEqual(candidate["optimizer_role"], "advisory_only")
        self.assertEqual(candidate["promotion"]["state"], "candidate")
        self.assertTrue(
            candidate["promotion"]["blockers"],
            "a candidate advisor must keep at least one explicit no-go blocker",
        )
        blockers = " ".join(candidate["promotion"]["blockers"]).lower()
        self.assertIn("no measured decision win", blockers)
        limitations = " ".join(candidate["model"]["limitations"]).lower()
        self.assertIn("linear elastostatics", limitations)
        self.assertIn("tet-only", limitations)
        # The runtime is explicit opt-in and default-off, not disabled: the
        # limitations must document the opt-in switch, the default-off runs, and
        # the advisory-only outcome.
        self.assertNotIn("disabled", limitations)
        self.assertIn("learned_advisor opt-in", limitations)
        self.assertIn("default it off", limitations)
        self.assertIn("remains advisory", limitations)

    def test_missing_provenance_is_rejected(self) -> None:
        error = self.invalid(
            lambda data: data["tracks"][0]["corpus"].pop("provenance_note")
        )
        self.assertIn("missing required provenance fields", str(error))

    def test_available_corpus_requires_manifest_and_dataset_hashes(self) -> None:
        error = self.invalid(
            lambda data: data["tracks"][0]["corpus"].update(
                {"manifest_sha256": None, "dataset_sha256": None}
            )
        )
        self.assertIn("manifest_sha256 must be sha256", str(error))
        self.assertIn("dataset_sha256 must be sha256", str(error))

    def test_leakage_prone_row_split_is_rejected(self) -> None:
        def mutate(data) -> None:
            split = data["tracks"][0]["split_policy"]
            split.update({"kind": "random", "group_by": "row", "held_out_unit": "row"})

        error = self.invalid(mutate)
        self.assertIn("leakage-prone", str(error))

    def test_absent_safety_vetoes_are_rejected(self) -> None:
        error = self.invalid(lambda data: data["tracks"][0].update({"safety_vetoes": []}))
        self.assertIn("at least one explicit veto", str(error))

    def test_unknown_simulation_family_is_rejected(self) -> None:
        error = self.invalid(
            lambda data: data["tracks"][0].update({"simulation_family": "quantum_cfd"})
        )
        self.assertIn("simulation_family is unknown", str(error))

    def test_promotion_without_model_and_evidence_hashes_is_rejected(self) -> None:
        def mutate(data) -> None:
            promotion = data["tracks"][1]["promotion"]
            promotion.update({"state": "promoted", "evidence_hashes": []})

        error = self.invalid(mutate)
        self.assertIn("requires a trained model", str(error))
        self.assertIn("requires evidence hashes", str(error))

    def test_trained_candidate_requires_artifact_and_model_card_hashes(self) -> None:
        def mutate(data) -> None:
            model = data["tracks"][0]["model"]
            model.update({"artifact_sha256": None, "model_card_sha256": None})

        error = self.invalid(mutate)
        self.assertIn("artifact_sha256 must be sha256", str(error))
        self.assertIn("model_card_sha256 must be sha256", str(error))

    def test_stale_baseline_source_hash_is_rejected_and_refreshable(self) -> None:
        data = copy.deepcopy(self.raw)
        data["tracks"][0]["deterministic_baseline"]["implementation_sha256"] = "0" * 64
        with self.assertRaises(RegistryValidationError) as caught:
            verify_baseline_source_hashes(data, REPO_ROOT)
        self.assertIn("implementation_sha256 is stale", str(caught.exception))
        self.assertGreaterEqual(refresh_baseline_source_hashes(data, REPO_ROOT), 1)
        verify_baseline_source_hashes(data, REPO_ROOT)


if __name__ == "__main__":
    unittest.main()
