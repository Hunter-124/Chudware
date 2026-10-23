"""mega-3 harness fixes — unit coverage.

Defects (live mega-2-fc-rocket + MegaRun3 gap-2 probe):
2. child_spawns=0: delegate missing from specify/plan/build tools_slot
3. xAI parallel read-timeout storm: unbounded concurrent complete_ex

Defect 1 of that list, "phase gate vs DAG progress: specify_incomplete while
BUILD/DELIVER done", is NOT covered here and no longer can be: its subject
`services/agent/loop.py::_drive_phases_from_dag` was deleted with the Python loop and nothing
re-implemented it. The `_drive` helper that monkeypatched it, and the manifest fixture
only that helper used, were never reached — removed 2026-08-05.
"""
from __future__ import annotations

import os
import threading
import time

import pytest


# ---------------------------------------------------------------------------
# 2. Spawn tool on specify / plan / build roles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role_id",
    [
        "spec_writer",
        "orchestrator",
        "layout_engineer",
        "feature_modeler",
        "assembly_fit_engineer",
        "department_manager",
    ],
)
def test_delegate_on_specify_plan_build_tools_slot(role_id):
    """GAP2: specify/plan/build roles must expose delegate on tools_slot."""
    from services.agent.roles import load_role

    card = load_role(role_id)
    tools = card.concrete_tools
    assert "delegate" in tools, f"{role_id} missing delegate: {tools}"
    tokens = card.allow_tokens
    assert "tool:delegate" in tokens or "delegate" in tokens or any(
        t == "delegate" or t.endswith(":delegate") for t in tokens
    ) or any(
        getattr(ref, "tool", None) == "delegate" for ref in card.tool_allowlist
    )


def test_spawn_depth_policy_permits_depth_gt0_for_capability_c():
    """Capability C max_spawn_depth=8; depth 1..8 ok, 9 refused."""
    from services.agent import role_models as rm

    assert rm.max_spawn_depth_for_capability("C") == 8
    for d in range(0, 9):
        pol = rm.check_spawn_depth_policy(d, letter="C", task_class="assembly")
        assert pol["ok"] is True, (d, pol)
    pol = rm.check_spawn_depth_policy(9, letter="C")
    assert pol["ok"] is False
    assert pol["max_spawn_depth"] == 8


def test_spec_writer_prompt_has_decomposition_nudge():
    from services.agent.roles import load_role

    card = load_role("spec_writer")
    assert "delegate" in card.system_prompt
    assert "parallel" in card.system_prompt.lower() or "child" in card.system_prompt.lower()


# ---------------------------------------------------------------------------
# 4. Residual mega-3 live fingerprints (t+630s)
# ---------------------------------------------------------------------------


def test_ledger_note_kind_manifest_confirm_aliases(tmp_path):
    """mega-3 residual: kind=manifest_confirm must not unknown-kind."""
    from services.agent.ledger import Ledger
    from services.agent.mission_dispatch import MissionDispatch

    led = Ledger("m3_mc", tmp_path / "ledger.json")
    md = MissionDispatch(run_id="m3_mc", ledger=led)
    out = md.ledger_note(
        kind="manifest_confirm",
        payload={"text": "5 deliverables confirmed", "name": "intake_confirm"},
        actor="n_intake",
    )
    assert out.get("ok") is True, out
    assert out.get("kind") == "decision"
    assert out.get("aliased_from") == "manifest_confirm"


@pytest.mark.parametrize(
    "section,canonical",
    [
        ("interfaces", "dimensions"),
        ("results", "artifacts"),
        ("notes", "stage_log"),
        ("plan", "build_nodes"),
        ("acceptance", "artifacts"),
    ],
)
def test_ledger_get_section_aliases_mega3(tmp_path, section, canonical):
    """mega-3 residual: section=interfaces|results|notes|plan|acceptance."""
    from services.agent.ledger import Ledger
    from services.agent.mission_dispatch import MissionDispatch

    led = Ledger("m3_sec", tmp_path / "ledger.json")
    md = MissionDispatch(run_id="m3_sec", ledger=led)
    out = md.ledger_get(section=section)
    assert out.get("ok") is True, out
    assert out.get("section") == canonical


# ---------------------------------------------------------------------------
# 3. xAI concurrent-request semaphore
# ---------------------------------------------------------------------------


def test_xai_max_concurrent_default_and_env(monkeypatch):
    from services.agent.providers import xai_sub_provider as xsp

    xsp._reset_request_semaphore_for_tests()
    monkeypatch.delenv("CHUDWARE_XAI_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("CHUDWARE_XAI_MAX_CONCURRENT", raising=False)
    assert xsp._max_concurrent_requests() == 3

    monkeypatch.setenv("CHUDWARE_XAI_MAX_CONCURRENT", "3")
    assert xsp._max_concurrent_requests() == 3
    xsp._reset_request_semaphore_for_tests()


def test_xai_read_timeout_default_and_env(monkeypatch):
    from services.agent.providers import xai_sub_provider as xsp

    monkeypatch.delenv("CHUDWARE_XAI_READ_TIMEOUT", raising=False)
    monkeypatch.delenv("CHUDWARE_XAI_READ_TIMEOUT", raising=False)
    assert xsp._read_timeout_s() == 180.0

    monkeypatch.setenv("CHUDWARE_XAI_READ_TIMEOUT", "240")
    assert xsp._read_timeout_s() == 240.0


def test_xai_complete_ex_bounds_concurrency(monkeypatch):
    """At most N concurrent _post_once calls under the semaphore."""
    from services.agent.providers import xai_sub_provider as xsp

    monkeypatch.setenv("CHUDWARE_XAI_MAX_CONCURRENT", "1")
    xsp._reset_request_semaphore_for_tests()

    in_flight = 0
    peak = 0
    lock = threading.Lock()

    class _Prov(xsp.XAISubProvider):
        def __init__(self):
            # skip real credential load
            self.model = "grok-4.5"
            self._cred = {
                "access_token": "t",
                "refresh_token": "r",
                "expires_at_ms": int(time.time() * 1000) + 3_600_000,
            }
            self.base_url = xsp.API_BASE_URL

        def _ensure_auth(self):
            return None

        def _post_once(self, body):
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            try:
                time.sleep(0.05)
            finally:
                with lock:
                    in_flight -= 1
            return {
                "choices": [
                    {"message": {"content": "ok", "reasoning_content": ""}}
                ]
            }

    p = _Prov()
    errors = []

    def _call():
        try:
            p.complete_ex("sys", [{"role": "user", "content": "hi"}], max_tokens=16)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, errors
    assert peak == 1, f"expected peak concurrency 1, got {peak}"
    xsp._reset_request_semaphore_for_tests()
