"""Hard CI guard for the translator training legs (T-F11 scope fence).

v0 is eval harness + gate + reward + serving scaffold. SFT/GRPO recipes MAY land but must
NEVER execute in CI, and must not start until the eval harness shows the ship gates are
reachable (RECOGNITION_RESIDUAL discipline extends to every domain — master plan §Pillar 13).
Same pattern as the T-H6/T-H10 training guards: explicit opt-in env var AND outside CI."""
from __future__ import annotations

import os

OPT_IN_ENV = "CHUDWARE_ALLOW_TRANSLATOR_TRAINING"
_CI_ENVS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "JENKINS_URL")


def in_ci() -> bool:
    return any(os.environ.get(k) for k in _CI_ENVS)


def training_allowed() -> tuple[bool, str]:
    if in_ci():
        return False, "CI detected — translator training never runs in CI (T-F11 scope fence)"
    if os.environ.get(OPT_IN_ENV) != "1":
        return False, (f"set {OPT_IN_ENV}=1 to run a training leg; blocked by default until "
                       "the eval harness shows the ship gates are reachable")
    return True, "explicit opt-in outside CI"


def assert_training_allowed() -> None:
    ok, reason = training_allowed()
    if not ok:
        raise RuntimeError(f"translator training blocked: {reason}")
