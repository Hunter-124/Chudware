from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from services.perception.config import ModelSpec
from services.perception.reference import NumpyMLDP, surprise_score
from .evaluate import evaluate_model
from .metrics import fit_event_threshold, precursor_event_scores
from .model import OUTPUT_NAMES, build_torch_model
from .synthetic import SyntheticCorpus, generate_synthetic_corpus

DEFAULT_SEED = 1729
TRAINING_EPOCHS = 800
LEARNING_RATE = 1.0e-2
GAUSSIAN_NLL_WEIGHT = 1.0
EVENT_AUX_WEIGHT = 0.2
WEIGHT_DECAY = 1.0e-5
BETA_NLL_BETA = 0.5
VARIANCE_STAGE_FRACTION = 0.5
PARITY_ATOL = 1.0e-5
SELECTION_FOLDS = 5


def _stratified_selection_folds(
    corpus: SyntheticCorpus,
    indices: np.ndarray,
    *,
    folds: int,
    seed: int,
) -> tuple[np.ndarray, ...]:
    """Partition development examples without consulting the locked test set."""
    if folds < 2 or len(indices) < folds:
        raise ValueError("selection folds require at least two non-empty folds")
    rng = np.random.default_rng(seed)
    strata: dict[tuple[str, bool], list[int]] = {}
    regimes = corpus.regimes or ("unknown",) * len(corpus.windows.window_ids)
    for index in indices:
        key = (regimes[int(index)], bool(corpus.windows.event_label[int(index)]))
        strata.setdefault(key, []).append(int(index))
    members: list[list[int]] = [[] for _ in range(folds)]
    next_fold = 0
    for key in sorted(strata):
        shuffled = np.asarray(strata[key], dtype=np.int64)
        rng.shuffle(shuffled)
        for index in shuffled:
            members[next_fold % folds].append(int(index))
            next_fold += 1
    return tuple(np.asarray(fold, dtype=np.int64) for fold in members)



def _split_indices(count: int, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    order = rng.permutation(count)
    train_end = max(1, int(count * 0.6))
    calib_end = max(train_end + 1, int(count * 0.8))
    return order[:train_end], order[train_end:calib_end], order[calib_end:]


def fit_reference_model(
    corpus: SyntheticCorpus,
    *,
    seed: int = DEFAULT_SEED,
    epochs: int = TRAINING_EPOCHS,
    _selection_enabled: bool = True,
    _fit_indices: tuple[np.ndarray, np.ndarray] | None = None,
    _candidate_models: dict[str, NumpyMLDP] | None = None,
) -> tuple[NumpyMLDP, dict[str, Any]]:
    """Train and calibration-select an uncertainty-honest causal model.

    Five-fold cross-validation selects the variance method across the complete
    development portion. Each held-out fold is untouched by that fold's model
    fit and calibration. The locked test split is not read here.
    """
    if epochs <= 0:
        raise ValueError("epochs must be positive")
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("torch is required for end-to-end MLDP training") from exc

    torch.manual_seed(seed)
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    initial = NumpyMLDP(corpus.spec, seed=seed)
    if _fit_indices is None:
        train_idx, calib_idx, _ = _split_indices(
            len(corpus.windows.window_ids), seed
        )
    else:
        train_idx, calib_idx = _fit_indices
    train = corpus.windows.subset(train_idx)
    calibration = corpus.windows.subset(calib_idx)

    input_names = (
        "cam_feat",
        "cam_mask",
        "cam_age",
        "proc_feat",
        "proc_mask",
        "proc_age",
        "cmd_future",
        "cmd_mask",
    )

    def tensors_for(batch: Any) -> tuple[Any, ...]:
        inputs = batch.model_inputs()
        return tuple(
            torch.from_numpy(np.asarray(inputs[name])) for name in input_names
        )

    train_tensors = tensors_for(train)
    calibration_tensors = tensors_for(calibration)
    target = torch.from_numpy(np.asarray(train.target_latent, np.float32))
    calibration_target = torch.from_numpy(
        np.asarray(calibration.target_latent, np.float32)
    )
    precursor_start = max(0, corpus.spec.window - 4)
    precursor_stop = min(
        corpus.spec.window, max(precursor_start + 1, corpus.spec.window - 2)
    )

    def event_targets_for(batch: Any) -> Any:
        labels = torch.from_numpy(np.asarray(batch.event_label, bool))
        values = torch.zeros(
            (len(batch.window_ids), corpus.spec.window, 3),
            dtype=torch.float32,
        )
        values[labels, precursor_start, 0] = 1.0
        values[labels, precursor_start:precursor_stop, 1] = 1.0
        values[labels, -1, 2] = 1.0
        return values

    event_target = event_targets_for(train)
    calibration_event_target = event_targets_for(calibration)
    positive_count = event_target.sum(dim=(0, 1))
    total_per_channel = float(event_target.shape[0] * event_target.shape[1])
    event_positive_weight = torch.where(
        positive_count > 0,
        (total_per_channel - positive_count)
        / positive_count.clamp_min(1.0),
        torch.ones_like(positive_count),
    ).clamp(max=64.0)

    encoder_mean_event_names = (
        "cam_w",
        "cam_b",
        "proc_w",
        "proc_b",
        "depth_w",
        "channel_w",
        "channel_b",
        "cmd_w",
        "horizon_emb",
        "forecast_w",
        "forecast_b",
        "event_w",
        "event_b",
    )
    variance_names = ("logvar_w", "logvar_b")
    jointly_learned_names = encoder_mean_event_names + variance_names

    def output_dict(torch_model: Any, tensors: tuple[Any, ...]) -> dict[str, Any]:
        return dict(zip(OUTPUT_NAMES, torch_model(*tensors)))

    def gaussian_nll(outputs: Mapping[str, Any], observed: Any) -> Any:
        residual = observed - outputs["z_hat"]
        return 0.5 * (
            residual.square() * torch.exp(-outputs["z_logvar"])
            + outputs["z_logvar"]
        ).mean()

    def event_loss(outputs: Mapping[str, Any], observed: Any) -> Any:
        return torch.nn.functional.binary_cross_entropy_with_logits(
            outputs["event_bnd"],
            observed,
            pos_weight=event_positive_weight,
        )

    def snapshot(torch_model: Any) -> dict[str, Any]:
        return {
            name: value.detach().clone()
            for name, value in torch_model.state_dict().items()
        }

    def train_joint(objective: str) -> tuple[Any, dict[str, float | int]]:
        torch.manual_seed(seed)
        torch_model = build_torch_model(initial).train()
        optimizer = torch.optim.AdamW(
            [getattr(torch_model, name) for name in jointly_learned_names],
            lr=LEARNING_RATE,
            weight_decay=WEIGHT_DECAY,
        )
        best_loss = float("inf")
        best_epoch = 0
        best_state: dict[str, Any] | None = None
        best_train_nll = 0.0
        best_event_loss = 0.0
        for epoch in range(1, epochs + 1):
            torch_model.train()
            optimizer.zero_grad(set_to_none=True)
            outputs = output_dict(torch_model, train_tensors)
            residual = target - outputs["z_hat"]
            nll_elements = 0.5 * (
                residual.square() * torch.exp(-outputs["z_logvar"])
                + outputs["z_logvar"]
            )
            if objective == "beta_nll":
                stopped_variance = torch.exp(
                    outputs["z_logvar"].detach()
                ).pow(BETA_NLL_BETA)
                forecast_loss = (nll_elements * stopped_variance).mean()
            elif objective == "gaussian_nll":
                forecast_loss = nll_elements.mean()
            else:
                raise AssertionError(f"unknown joint objective: {objective}")
            auxiliary = event_loss(outputs, event_target)
            loss = (
                GAUSSIAN_NLL_WEIGHT * forecast_loss
                + EVENT_AUX_WEIGHT * auxiliary
            )
            loss.backward()
            torch.nn.utils.clip_grad_norm_(
                [getattr(torch_model, name) for name in jointly_learned_names],
                5.0,
            )
            optimizer.step()

            torch_model.eval()
            with torch.no_grad():
                validation_outputs = output_dict(
                    torch_model, calibration_tensors
                )
                validation_nll = gaussian_nll(
                    validation_outputs, calibration_target
                )
                validation_event = event_loss(
                    validation_outputs, calibration_event_target
                )
                validation_loss = float(
                    validation_nll + EVENT_AUX_WEIGHT * validation_event
                )
            if validation_loss < best_loss:
                best_loss = validation_loss
                best_epoch = epoch
                best_train_nll = float(nll_elements.mean().detach())
                best_event_loss = float(auxiliary.detach())
                best_state = snapshot(torch_model)
        if best_state is None:
            raise AssertionError(f"{objective} did not produce a finite checkpoint")
        torch_model.load_state_dict(best_state)
        torch_model.eval()
        return torch_model, {
            "best_epoch": best_epoch,
            "best_validation_objective": best_loss,
            "train_gaussian_nll_at_checkpoint": best_train_nll,
            "train_event_loss_at_checkpoint": best_event_loss,
        }

    joint_model, joint_training = train_joint("gaussian_nll")
    beta_model, beta_training = train_joint("beta_nll")

    torch.manual_seed(seed)
    two_stage_model = build_torch_model(initial).train()
    mean_optimizer = torch.optim.AdamW(
        [
            getattr(two_stage_model, name)
            for name in encoder_mean_event_names
        ],
        lr=LEARNING_RATE,
        weight_decay=WEIGHT_DECAY,
    )
    best_mean_loss = float("inf")
    best_mean_epoch = 0
    best_mean_state: dict[str, Any] | None = None
    for epoch in range(1, epochs + 1):
        two_stage_model.train()
        mean_optimizer.zero_grad(set_to_none=True)
        outputs = output_dict(two_stage_model, train_tensors)
        mse = (target - outputs["z_hat"]).square().mean()
        auxiliary = event_loss(outputs, event_target)
        loss = mse + EVENT_AUX_WEIGHT * auxiliary
        loss.backward()
        torch.nn.utils.clip_grad_norm_(
            [
                getattr(two_stage_model, name)
                for name in encoder_mean_event_names
            ],
            5.0,
        )
        mean_optimizer.step()
        two_stage_model.eval()
        with torch.no_grad():
            validation_outputs = output_dict(
                two_stage_model, calibration_tensors
            )
            validation_mse = (
                calibration_target - validation_outputs["z_hat"]
            ).square().mean()
            validation_event = event_loss(
                validation_outputs, calibration_event_target
            )
            validation_loss = float(
                validation_mse + EVENT_AUX_WEIGHT * validation_event
            )
        if validation_loss < best_mean_loss:
            best_mean_loss = validation_loss
            best_mean_epoch = epoch
            best_mean_state = snapshot(two_stage_model)
    if best_mean_state is None:
        raise AssertionError("two-stage mean fit did not produce a checkpoint")
    two_stage_model.load_state_dict(best_mean_state)

    for name, parameter in two_stage_model.named_parameters():
        parameter.requires_grad_(name in variance_names)
    variance_optimizer = torch.optim.AdamW(
        [getattr(two_stage_model, name) for name in variance_names],
        lr=LEARNING_RATE,
        weight_decay=WEIGHT_DECAY,
    )
    variance_epochs = max(1, int(epochs * VARIANCE_STAGE_FRACTION))
    best_variance_nll = float("inf")
    best_variance_epoch = 0
    best_variance_state: dict[str, Any] | None = None
    for epoch in range(1, variance_epochs + 1):
        two_stage_model.train()
        variance_optimizer.zero_grad(set_to_none=True)
        outputs = output_dict(two_stage_model, train_tensors)
        nll = gaussian_nll(outputs, target)
        nll.backward()
        torch.nn.utils.clip_grad_norm_(
            [getattr(two_stage_model, name) for name in variance_names],
            5.0,
        )
        variance_optimizer.step()
        two_stage_model.eval()
        with torch.no_grad():
            validation_nll = float(
                gaussian_nll(
                    output_dict(two_stage_model, calibration_tensors),
                    calibration_target,
                )
            )
        if validation_nll < best_variance_nll:
            best_variance_nll = validation_nll
            best_variance_epoch = epoch
            best_variance_state = snapshot(two_stage_model)
    if best_variance_state is None:
        raise AssertionError("two-stage variance fit did not produce a checkpoint")
    two_stage_model.load_state_dict(best_variance_state)
    two_stage_model.eval()

    joint_reference = joint_model.to_numpy_reference()
    joint_calibration_output = joint_reference.forward(
        calibration.model_inputs()
    )
    squared_standardized_residual = (
        (calibration.target_latent - joint_calibration_output["z_hat"]) ** 2
        * np.exp(-joint_calibration_output["z_logvar"])
    )
    scalar_temperature = max(
        float(np.mean(squared_standardized_residual)), 1.0e-6
    )
    horizon_temperatures = np.maximum(
        np.mean(squared_standardized_residual, axis=(0, 2)),
        1.0e-6,
    ).astype(np.float32)

    calibration_logvar_tensor = torch.from_numpy(
        np.asarray(joint_calibration_output["z_logvar"], np.float32)
    )
    calibration_mean_tensor = torch.from_numpy(
        np.asarray(joint_calibration_output["z_hat"], np.float32)
    )
    affine_slope = torch.ones((), dtype=torch.float32, requires_grad=True)
    affine_offset = torch.zeros(
        (1, len(corpus.spec.horizons), 1, corpus.spec.latent_features),
        dtype=torch.float32,
        requires_grad=True,
    )
    affine_optimizer = torch.optim.LBFGS(
        [affine_slope, affine_offset],
        lr=0.5,
        max_iter=200,
        line_search_fn="strong_wolfe",
    )

    def affine_closure() -> Any:
        affine_optimizer.zero_grad()
        adjusted_logvar = (
            affine_slope * calibration_logvar_tensor + affine_offset
        )
        residual = calibration_target - calibration_mean_tensor
        loss = 0.5 * (
            residual.square() * torch.exp(-adjusted_logvar)
            + adjusted_logvar
        ).mean()
        loss.backward()
        return loss

    affine_optimizer.step(affine_closure)
    affine_slope_value = float(affine_slope.detach())
    affine_offset_value = affine_offset.detach().cpu().numpy().reshape(
        len(corpus.spec.horizons), corpus.spec.latent_features
    )
    affine_calibration_logvar = (
        affine_slope_value * joint_calibration_output["z_logvar"]
        + affine_offset_value[None, :, None, :]
    )
    absolute_standardized = np.abs(
        calibration.target_latent - joint_calibration_output["z_hat"]
    ) * np.exp(-0.5 * affine_calibration_logvar)
    conformal_2sigma_temperature = max(
        float(
            np.quantile(
                absolute_standardized / 2.0,
                0.954499736,
                method="linear",
            )
            ** 2
        ),
        1.0e-6,
    )

    def with_logvar_temperature(
        base: NumpyMLDP, log_temperature: np.ndarray | float
    ) -> NumpyMLDP:
        parameters = {
            name: np.asarray(value, np.float32).copy()
            for name, value in base.parameters.items()
        }
        parameters["logvar_b"] += np.asarray(log_temperature, np.float32)
        return NumpyMLDP(
            base.spec,
            seed=base.seed,
            parameters=parameters,
        )

    def with_affine_conformal_temperature(base: NumpyMLDP) -> NumpyMLDP:
        parameters = {
            name: np.asarray(value, np.float32).copy()
            for name, value in base.parameters.items()
        }
        parameters["logvar_w"] *= np.float32(affine_slope_value)
        parameters["logvar_b"] *= np.float32(affine_slope_value)
        parameters["logvar_b"] += affine_offset_value.astype(np.float32)
        parameters["logvar_b"] += np.float32(
            np.log(conformal_2sigma_temperature)
        )
        return NumpyMLDP(
            base.spec,
            seed=base.seed,
            parameters=parameters,
        )

    candidates = {
        "beta_nll": beta_model.to_numpy_reference(),
        "two_stage": two_stage_model.to_numpy_reference(),
        "scalar_temperature": with_logvar_temperature(
            joint_reference, float(np.log(scalar_temperature))
        ),
        "per_horizon_temperature": with_logvar_temperature(
            joint_reference, np.log(horizon_temperatures)
        ),
        "affine_conformal_temperature": with_affine_conformal_temperature(
            joint_reference
        ),
    }
    if _candidate_models is not None:
        _candidate_models.update(candidates)

    def numpy_nll(candidate: NumpyMLDP, batch: Any) -> float:
        output = candidate.forward(batch.model_inputs())
        residual = batch.target_latent - output["z_hat"]
        return float(
            np.mean(
                0.5
                * (
                    residual**2 * np.exp(-output["z_logvar"])
                    + output["z_logvar"]
                )
            )
        )

    candidate_nll = {
        name: numpy_nll(candidate, calibration)
        for name, candidate in candidates.items()
    }
    calibration_parameter_count = {
        "beta_nll": 0,
        "two_stage": 0,
        "scalar_temperature": 1,
        "per_horizon_temperature": len(corpus.spec.horizons),
        "affine_conformal_temperature": (
            2
            + len(corpus.spec.horizons) * corpus.spec.latent_features
        ),
    }
    if _selection_enabled:
        _, _, test_idx = _split_indices(
            len(corpus.windows.window_ids), seed
        )
        development_idx = np.setdiff1d(
            np.arange(len(corpus.windows.window_ids), dtype=np.int64),
            test_idx,
            assume_unique=True,
        )
        selection_folds = _stratified_selection_folds(
            corpus,
            development_idx,
            folds=SELECTION_FOLDS,
            seed=seed + 1,
        )
        selection_fold_examples = [
            len(held_out_idx) for held_out_idx in selection_folds
        ]
        fold_nll: dict[str, list[float]] = {
            name: [] for name in candidates
        }
        fold_winners: list[str] = []
        for fold_number, held_out_idx in enumerate(selection_folds):
            outer_train_idx = np.setdiff1d(
                development_idx, held_out_idx, assume_unique=True
            )
            inner_rng = np.random.default_rng(seed + 100 + fold_number)
            inner_order = inner_rng.permutation(outer_train_idx)
            inner_calibration_count = max(
                1, int(round(len(inner_order) * 0.2))
            )
            inner_calib_idx = inner_order[:inner_calibration_count]
            inner_train_idx = inner_order[inner_calibration_count:]
            fold_candidates: dict[str, NumpyMLDP] = {}
            fit_reference_model(
                corpus,
                seed=seed,
                epochs=epochs,
                _selection_enabled=False,
                _fit_indices=(inner_train_idx, inner_calib_idx),
                _candidate_models=fold_candidates,
            )
            held_out = corpus.windows.subset(held_out_idx)
            scores = {
                name: numpy_nll(candidate, held_out)
                for name, candidate in fold_candidates.items()
            }
            for name, score in scores.items():
                fold_nll[name].append(score)
            fold_winners.append(
                min(scores, key=lambda name: (scores[name], name))
            )
        mean_nll = {
            name: float(np.mean(scores))
            for name, scores in fold_nll.items()
        }
        standard_error = {
            name: float(
                np.std(scores, ddof=1) / np.sqrt(len(scores))
            )
            for name, scores in fold_nll.items()
        }
        raw_winner = min(
            mean_nll, key=lambda name: (mean_nll[name], name)
        )
        paired_difference: dict[str, float] = {}
        paired_difference_se: dict[str, float] = {}
        tied_methods: list[str] = []
        raw_scores = np.asarray(fold_nll[raw_winner], np.float64)
        for name, scores in fold_nll.items():
            differences = np.asarray(scores, np.float64) - raw_scores
            paired_difference[name] = float(np.mean(differences))
            paired_difference_se[name] = (
                0.0
                if name == raw_winner
                else float(
                    np.std(differences, ddof=1)
                    / np.sqrt(len(differences))
                )
            )
            if (
                paired_difference[name]
                <= paired_difference_se[name]
            ):
                tied_methods.append(name)
        selected_method = min(
            tied_methods,
            key=lambda name: (
                calibration_parameter_count[name],
                mean_nll[name],
                name,
            ),
        )
        ranking = sorted(
            mean_nll, key=lambda name: (mean_nll[name], name)
        )
        raw_runner_up = ranking[1]
        top_pair_difference = (
            mean_nll[raw_runner_up] - mean_nll[raw_winner]
        )
        raw_top_pair_differences = np.asarray(
            fold_nll[raw_runner_up], np.float64
        ) - np.asarray(fold_nll[raw_winner], np.float64)
        top_pair_difference_se = float(
            np.std(raw_top_pair_differences, ddof=1)
            / np.sqrt(len(raw_top_pair_differences))
        )
        top_pair_tied = top_pair_difference <= top_pair_difference_se
        runner_up = min(
            (name for name in ranking if name != selected_method),
            key=lambda name: (mean_nll[name], name),
        )
        selected_difference = np.asarray(
            fold_nll[selected_method], np.float64
        ) - np.asarray(fold_nll[runner_up], np.float64)
        selection_difference_se = float(
            np.std(selected_difference, ddof=1)
            / np.sqrt(len(selected_difference))
        )
        fold_choice_count = {
            name: fold_winners.count(name) for name in candidates
        }
        selection_decision = (
            "decisive" if len(tied_methods) == 1 else "parsimony_tie_break"
        )
        selection_stable = (
            fold_choice_count[selected_method] == SELECTION_FOLDS
        )
    else:
        fold_nll = {
            name: [score] for name, score in candidate_nll.items()
        }
        mean_nll = dict(candidate_nll)
        standard_error = {name: 0.0 for name in candidates}
        paired_difference = {name: 0.0 for name in candidates}
        selection_fold_examples = [len(calib_idx)]
        paired_difference_se = {name: 0.0 for name in candidates}
        ranking = sorted(
            mean_nll, key=lambda name: (mean_nll[name], name)
        )
        raw_winner = ranking[0]
        raw_runner_up = ranking[1]
        top_pair_difference = (
            mean_nll[raw_runner_up] - mean_nll[raw_winner]
        )
        top_pair_difference_se = 0.0
        top_pair_tied = False
        selected_method = raw_winner
        runner_up = raw_runner_up
        tied_methods = [selected_method]
        fold_winners = [selected_method]
        fold_choice_count = {
            name: int(name == selected_method) for name in candidates
        }
        selection_difference_se = 0.0
        selection_decision = "internal_fit"
        selection_stable = True
    model = candidates[selected_method]

    calibration_output = model.forward(calibration.model_inputs())
    scores = surprise_score(
        calibration.target_latent,
        calibration_output["z_hat"],
        calibration_output["z_logvar"],
    )
    normal = scores[~calibration.event_label]
    if normal.size == 0:
        normal = scores
    center = float(np.quantile(normal, 0.99, method="higher"))
    mad = float(np.median(np.abs(normal - np.median(normal))))
    scale = max(mad * 1.4826, 1.0e-3)

    calibration_event_scores = precursor_event_scores(
        calibration_output["event_bnd"]
    )
    normal_event_scores = calibration_event_scores[
        ~calibration.event_label
    ]
    if normal_event_scores.size == 0:
        normal_event_scores = calibration_event_scores
    event_threshold = fit_event_threshold(normal_event_scores)

    selected_torch_model = build_torch_model(model).eval()
    with torch.no_grad():
        torch_outputs = selected_torch_model(*calibration_tensors)
    parity_max_abs_error = max(
        float(
            np.max(
                np.abs(
                    value.detach().cpu().numpy()
                    - calibration_output[name]
                )
            )
        )
        for name, value in zip(OUTPUT_NAMES, torch_outputs)
    )
    if parity_max_abs_error > PARITY_ATOL:
        raise AssertionError(
            "exported numpy reference differs from Torch: "
            f"{parity_max_abs_error} > {PARITY_ATOL}"
        )

    method_comparison: dict[str, dict[str, Any]] = {
        name: {
            "fold_nll": fold_nll[name],
            "mean_oof_nll": mean_nll[name],
            "standard_error": standard_error[name],
            "paired_mean_nll_difference_from_raw_best": (
                paired_difference[name]
            ),
            "paired_difference_standard_error": (
                paired_difference_se[name]
            ),
            "statistically_tied_with_raw_best": name in tied_methods,
            "fold_choice_count": fold_choice_count[name],
            "fold_choice_frequency": (
                fold_choice_count[name] / len(fold_winners)
            ),
            "calibration_parameter_count": (
                calibration_parameter_count[name]
            ),
            "final_fit_calibration_nll": candidate_nll[name],
            "selected": name == selected_method,
        }
        for name in ranking
    }
    method_comparison["beta_nll"].update(
        {"beta": BETA_NLL_BETA, **beta_training}
    )
    method_comparison["two_stage"].update(
        {
            "mean_best_epoch": best_mean_epoch,
            "mean_best_validation_objective": best_mean_loss,
            "variance_best_epoch": best_variance_epoch,
            "variance_best_calibration_nll": best_variance_nll,
        }
    )
    method_comparison["scalar_temperature"]["variance_temperature"] = (
        scalar_temperature
    )
    method_comparison["per_horizon_temperature"][
        "variance_temperatures_by_horizon_and_latent"
    ] = horizon_temperatures.tolist()
    method_comparison["affine_conformal_temperature"].update(
        {
            "logvar_affine_slope": affine_slope_value,
            "logvar_affine_offsets_by_horizon_and_latent": (
                affine_offset_value.tolist()
            ),
            "conformal_target_coverage": 0.954499736,
            "conformal_sigma": 2.0,
            "conformal_variance_temperature": conformal_2sigma_temperature,
        }
    )

    return model, {
        "surprise_center": center,
        "surprise_scale": scale,
        "event_threshold": event_threshold,
        "event_score_source": "max_precursor_probability",
        "event_threshold_quantile": 0.99,
        "gaussian_nll_weight": GAUSSIAN_NLL_WEIGHT,
        "event_aux_weight": EVENT_AUX_WEIGHT,
        "training_epochs": epochs,
        "learning_rate": LEARNING_RATE,
        "weight_decay": WEIGHT_DECAY,
        "selection_split": (
            "five_fold_cross_validation_over_development_train"
            if _selection_enabled
            else "internal_fit_validation"
        ),
        "selection_folds": len(fold_winners),
        "selection_examples": (
            len(development_idx) if _selection_enabled else len(calib_idx)
        ),
        "selection_fold_examples": selection_fold_examples,
        "selection_fold_winners": fold_winners,
        "selection_fold_winner_counts": fold_choice_count,
        "selection_stable_across_folds": selection_stable,
        "selection_decision": selection_decision,
        "selection_tied_methods": tied_methods,
        "selection_raw_nll_winner": raw_winner,
        "selection_raw_nll_runner_up": raw_runner_up,
        "selection_top_pair_nll_difference": top_pair_difference,
        "selection_top_pair_difference_standard_error": (
            top_pair_difference_se
        ),
        "selection_top_pair_statistically_tied": top_pair_tied,
        "selection_decisive_and_stable": (
            selection_decision == "decisive" and selection_stable
        ),
        "calibration_examples": len(calib_idx),
        "test_used_for_selection": False,
        "selected_variance_method": selected_method,
        "selection_calibration_nll": mean_nll[selected_method],
        "selection_calibration_nll_standard_error": (
            standard_error[selected_method]
        ),
        "selection_runner_up": runner_up,
        "selection_nll_margin": abs(
            mean_nll[runner_up] - mean_nll[selected_method]
        ),
        "selection_nll_difference_standard_error": (
            selection_difference_se
        ),
        "variance_method_comparison": method_comparison,
        "mean_event_parameters_unchanged_by_calibration": selected_method in {
            "scalar_temperature",
            "per_horizon_temperature",
            "affine_conformal_temperature",
        },
        "joint_training": joint_training,
        "parity_atol": PARITY_ATOL,
        "parity_max_abs_error": parity_max_abs_error,
        "conditional_logvar_weight_l2": float(
            np.linalg.norm(model.parameters["logvar_w"])
        ),
    }


def train_job(
    job: Mapping[str, Any] | None = None,
    recipe: Mapping[str, Any] | None = None,
    out_dir: str | Path = "scratch/perception",
) -> dict[str, Any]:
    """Train/evaluate a tiny CPU model; compatible with the flywheel call shape."""
    job, recipe = dict(job or {}), dict(recipe or {})
    seed = int(job.get("seed", recipe.get("seed", DEFAULT_SEED)))
    count = int(job.get("synthetic_windows", recipe.get("synthetic_windows", 96)))
    corpus = generate_synthetic_corpus(windows=count, seed=seed)
    model, calibration = fit_reference_model(corpus, seed=seed)
    _, _, test_idx = _split_indices(count, seed)
    metrics = evaluate_model(model, corpus.windows.subset(test_idx), calibration=calibration)
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    model.save(destination / "mldp_reference.npz")
    result = {
        "seed": seed,
        "feature_spec_id": model.spec.feature_spec_id,
        "calibration": calibration,
        "metrics": metrics,
    }
    (destination / "metrics.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train the tiny deterministic MLDP reference model")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--windows", type=int, default=96)
    args = parser.parse_args(argv)
    result = train_job({"seed": args.seed, "synthetic_windows": args.windows}, {}, args.out)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
