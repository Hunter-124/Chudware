"""Hard CI guard for the VLM training legs (T-H10 scope fence).

v0 is bench + corpus + zero-shot baselines ONLY. The SFT/DPO/GRPO recipes in this
directory MAY land but must NEVER execute in CI, and must not start at all until the T-H8
VLM bench shows a real measured residual (RECOGNITION_RESIDUAL discipline). This guard is
the enforcement: every recipe runner calls `assert_training_allowed()` as its first line.

Mirrors the T-H6 fleet-runner GRPO guard: training runs only with an EXPLICIT opt-in env
var AND outside CI. Absent that, importing/dry-running a recipe is fine (configs load,
reward is computable, tests pass) but a real training step raises."""
from __future__ import annotations

import os

OPT_IN_ENV = "AICAD_ALLOW_VLM_TRAINING"
_CI_ENVS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "JENKINS_URL")


def in_ci() -> bool:
    return any(os.environ.get(k) for k in _CI_ENVS)


def training_allowed() -> tuple[bool, str]:
    if in_ci():
        return False, "CI detected — VLM training never runs in CI (T-H10 scope fence)"
    if os.environ.get(OPT_IN_ENV) != "1":
        return False, (f"set {OPT_IN_ENV}=1 to run a training leg; blocked by default until "
                       "the T-H8 VLM bench residual gates GO (RECOGNITION_RESIDUAL)")
    return True, "explicit opt-in outside CI"


def assert_training_allowed() -> None:
    ok, reason = training_allowed()
    if not ok:
        raise RuntimeError(f"VLM training blocked: {reason}")
