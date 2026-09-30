import pytest

from training.viewpoint.utility.evaluate import ab_arm_table
from training.viewpoint.utility.store import TrialStore


def test_changed_model_arm_uses_isolated_membership_with_shared_cached_labels(tmp_path):
    old = TrialStore(tmp_path, name="ab_old_weights")
    current = TrialStore(tmp_path, name="ab_new_weights")
    old.append_sets([{"set_key": "old", "part_id": "p", "split": "test", "k": 4,
                      "strategies": ["model_greedy"]}])
    current.append_sets([
        {"set_key": "new", "part_id": "p", "split": "test", "k": 4,
         "strategies": ["model_greedy"]},
        {"set_key": "hand", "part_id": "p", "split": "test", "k": 4,
         "strategies": ["hand_greedy"]},
    ])
    current.append_labels("independent", [
        {"set_key": key, "qid": "q", "p_correct": p, "correct": True}
        for key, p in (("new", .8), ("hand", .5), ("old", .1))
    ])
    table = ab_arm_table(current, n_boot=100)
    delta = table["paired_deltas"]["model_greedy@4 - hand_greedy@4"]
    assert delta["estimate"] == pytest.approx(.3)
    assert delta["n"] == 1
    assert ab_arm_table(old, n_boot=100)["accuracy"]["model_greedy@4"]["p_correct"][
        "estimate"] == pytest.approx(.1)


def test_val_comparison_filters_split_and_pairs_part_query_identity(tmp_path):
    store = TrialStore(tmp_path)
    records, labels = [], []
    for pid, split, model_p in (("a", "val", .8), ("b", "val", .6), ("c", "test", .1)):
        for strategy, p in (("model_greedy", model_p), ("hand_greedy", .5)):
            key = f"{pid}:{strategy}"
            records.append({"set_key": key, "part_id": pid, "split": split,
                            "k": 4, "strategies": [strategy]})
            labels.append({"set_key": key, "qid": "shared-q", "p_correct": p,
                           "correct": True})
    store.append_sets(records)
    store.append_labels("independent", labels)
    delta = ab_arm_table(store, split="val", n_boot=100)["paired_deltas"][
        "model_greedy@4 - hand_greedy@4"]
    assert delta["estimate"] == pytest.approx(.2)
    assert delta["n"] == 2
    assert delta["n_parts"] == 2
