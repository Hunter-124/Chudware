"""Real-kernel quota prerequisites shared by the cell runtime and quota tests.

Root applies a disposable XFS/ext4 plan with ``provision_cell_quota.py --apply``
and exports both paths:

- ``CHUDWARE_TEST_QUOTA_PLAN``: the root-owned plan. Its ``purpose`` is
  ``disposable-kernel-quota-smoke`` and it lists at least two owners, each with
  one bounded project group.
- ``CHUDWARE_TEST_QUOTA_PROOF_ROOT``: the proof root the plan was applied to.

Admission tests run as the plan's unprivileged ``service_uid``; the
provisioning test runs as real root in a separate invocation. Without both
paths every real-kernel test skips. Nothing here mounts a filesystem, invokes
sudo, or bypasses admission.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cell"))
import storage_quota as quota


def disposable_quota_plan(*, root: bool = False) -> tuple[dict, str]:
    """Return the validated disposable plan and its proof root, or skip."""
    plan_path = os.environ.get("CHUDWARE_TEST_QUOTA_PLAN")
    proof_root = os.environ.get("CHUDWARE_TEST_QUOTA_PROOF_ROOT")
    if not plan_path and not proof_root:
        pytest.skip("real kernel quota prerequisite unavailable: root must provision a disposable XFS/ext4 plan")
    assert plan_path and proof_root, "both explicit real-kernel quota setup paths are required"
    if root and (os.geteuid() != 0 or os.getuid() != 0):
        pytest.skip("provisioning smoke runs the root quota tool; rerun it as real root")
    if not root and os.geteuid() == 0:
        pytest.skip("admission smoke runs as the plan's unprivileged service uid, not root")
    plan = quota.validate_policy(quota.read_authority(plan_path), quota.PLAN_SCHEMA)
    assert plan.get("purpose") == "disposable-kernel-quota-smoke", "never exhaust real workspace storage"
    if not root:
        assert os.getuid() == plan["service_uid"] != 0, "kernel smoke must run as the unprivileged cell service uid"
    assert len(plan["owners"]) >= 2, "two independently charged owners required"
    assert all(len(owner["groups"]) == 1 for owner in plan["owners"][:2]), "single scratch volume per owner required"
    assert all(owner["groups"][0]["hard_bytes"] <= 1024 ** 3 for owner in plan["owners"][:2]), "bounded smoke budgets required"
    return plan, proof_root


def owner_roots(owner: dict) -> tuple[str, Path, Path]:
    """Workspace key, workspace root and cell-state root of an owner's first workspace."""
    roots = owner["groups"][0]["roots"]
    workspace = next(root for root in roots if root["kind"] == "workspace")
    key = workspace["workspace_key"]
    state = next(root for root in roots if root["workspace_key"] == key and root["kind"] == "state")
    return key, Path(workspace["path"]), Path(state["path"])
