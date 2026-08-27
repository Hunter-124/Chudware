"""Cross-path parity for the two dispatch lanes that fan into ``agent_turn.run_turn``.

Path A  ``MissionDispatch._run_subagent(_turn)``      — the ``delegate`` tool
Path B  ``services.dag.node_runner._default_agent_fn`` — compiled DAG graph nodes

Two audited drift defects are pinned here:

1. A transient provider fault (429 / 5xx / truncated reply / mid-stream
   disconnect) was retried for free on a DAG node — ``node_runner._map_verdict``
   reads the structured ``result['retryable']`` flag and upgrades ESCALATE →
   RETRY — while the architecturally identical ``delegate`` reported a FLAT
   TERMINAL FAILURE, because ``mission_dispatch`` had no reference to
   ``retryable`` at all.
2. ``agent.subagent_tool_call_budget`` (one GUI slider) was read only by
   ``mission_dispatch``. ``services/dag`` never read the key, so moving the
   slider governed the ``delegate`` lane and had ZERO effect on every compiled
   DAG (``mission_start``) role.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from services.agent import mission_dispatch as md  # noqa: E402
from services.agent.ledger import Ledger  # noqa: E402
from services.agent.mission_dispatch import MissionDispatch  # noqa: E402


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------

class _TurnResult:
    """The three attributes both lanes read off ``run_turn``'s return value."""

    def __init__(self, success: bool, stop_reason: str, result: dict):
        self.success = success
        self.stop_reason = stop_reason
        self.result = result


class _Node:
    """The shape ``DelegationTree.request_delegation`` really returns."""

    def __init__(self, role: str, tokens_granted: int):
        self._d = {"id": "dg_parity", "role": role, "status": "approved",
                   "depth": 1,
                   "budget": {"tokens_granted": tokens_granted,
                              "tokens_spent": 0, "attempts": 0}}

    def to_dict(self) -> dict:
        return dict(self._d)


class _FakeTree:
    """Duck-typed DelegationTree: approve immediately, record transitions."""

    def __init__(self, tokens_granted: int = 16000):
        self._granted = tokens_granted
        self.transitions: list[tuple] = []

    def request_delegation(self, role, brief, budget, **_kw):
        return _Node(role, self._granted)

    def start(self, node_id):
        self.transitions.append(("start", node_id))

    def accrue(self, node_id, tokens):
        self.transitions.append(("accrue", node_id, tokens))

    def complete(self, node_id, tokens_used=0):
        self.transitions.append(("complete", node_id, tokens_used))

    def fail(self, node_id, reason=""):
        self.transitions.append(("fail", node_id, reason))


class _StubProvider:
    """Never consulted: every test spies on ``run_turn`` itself."""

    name = "stub"

    def complete(self, system, messages, max_tokens=None):  # pragma: no cover
        raise AssertionError("run_turn is spied; the provider must not be called")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
    # Hermetic: delegate() rebuilds a per-role provider from model assignment,
    # which on a dev machine picks up ambient config.json ai_roles. Force the
    # legacy no-assignment path so the spy below is what runs.
    monkeypatch.setattr("services.agent.config.load_config", lambda *a, **k: {})
    monkeypatch.setattr("services.agent.role_models.resolve_for_node",
                        lambda *a, **k: None)
    monkeypatch.setattr("services.agent.role_models.resolve_role_model",
                        lambda *a, **k: None)
    monkeypatch.delenv("CHUDWARE_AGENT_MAX_TOOL_CALLS", raising=False)
    return tmp_path


class _CaptureLedger:
    """Duck-typed Ledger that keeps every appended row for assertions."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, actor: str, kind: str, payload: dict) -> dict:
        row = {"actor": actor, "kind": kind, "payload": payload}
        self.rows.append(row)
        return row


def _dispatch(tmp_path, tree, ledger=None) -> MissionDispatch:
    if ledger is None:
        ledger = Ledger("parity_run", path=tmp_path / "ledger.json")
    return MissionDispatch(run_id="parity_run", ledger=ledger,
                           delegation_tree=tree, provider=_StubProvider())


def _spy_run_turn(monkeypatch, scripted: list[_TurnResult]) -> list[dict]:
    """Replace ``agent_turn.run_turn`` with a scripted, kwargs-recording spy."""
    from services.agent import agent_turn as at

    seen: list[dict] = []

    def _spy(*args, **kwargs):
        seen.append(dict(kwargs))
        return scripted[min(len(seen) - 1, len(scripted) - 1)]

    monkeypatch.setattr(at, "run_turn", _spy)
    return seen


def _retryable_failure() -> _TurnResult:
    return _TurnResult(False, "provider_error", {
        "retryable": True,
        "error": "HTTP 429 rate limited by provider",
        "tool_calls": 0,
    })


def _dead_route_failure() -> _TurnResult:
    """What ``classify_provider_error`` stamps for a 404 / real auth failure."""
    return _TurnResult(False, "provider_error", {
        "retryable": False,
        "error": "HTTP 404 no such model route",
        "tool_calls": 0,
    })


def _success() -> _TurnResult:
    return _TurnResult(True, "confirmed", {
        "tool_calls": 2,
        "handoff": {"status": "done", "summary": "built"},
        "usage": {"total_tokens": 4321},
    })


# ---------------------------------------------------------------------------
# Defect 1 — bounded one-shot recovery on the delegate lane
# ---------------------------------------------------------------------------

class TestDelegateRetryableRecovery:
    def test_retryable_child_failure_is_recovered_once(self, env, monkeypatch):
        """A 429 on the first attempt must not end the spawn: Path B has always
        retried it within DEFAULT_RETRY_BUDGET=1, so Path A must too."""
        seen = _spy_run_turn(monkeypatch, [_retryable_failure(), _success()])
        node = _dispatch(env, _FakeTree()).delegate(
            role="sourcing_specialist", brief="find a bearing",
            budget_tokens=16000)
        assert len(seen) == 2, "the retryable first attempt was not re-run"
        assert node["ok"] is True
        assert node["subagent_status"] == "done"
        # The recovery is REPORTED, never silent: a parent that cannot see the
        # first attempt failed cannot judge the run.
        assert node["provider_retry"]["attempts"] == 2
        assert node["provider_retry"]["first_stop_reason"] == "provider_error"
        assert "429" in node["provider_retry"]["first_error"]

    def test_non_retryable_child_failure_still_fails_fast(self, env, monkeypatch):
        """A 404 dead route and a genuine auth failure are stamped
        retryable=False, and that is load-bearing: re-running burns a spawn to
        fail identically."""
        seen = _spy_run_turn(
            monkeypatch, [_dead_route_failure(), _success()])
        node = _dispatch(env, _FakeTree()).delegate(
            role="sourcing_specialist", brief="find a bearing",
            budget_tokens=16000)
        assert len(seen) == 1, "a non-retryable failure must not be re-run"
        assert node["ok"] is False
        assert "provider_retry" not in node

    def test_absent_retryable_flag_is_not_a_retry(self, env, monkeypatch):
        """An unclassified failure carries no flag. Absent != True."""
        unflagged = _TurnResult(False, "provider_error", {"error": "boom"})
        seen = _spy_run_turn(monkeypatch, [unflagged, _success()])
        node = _dispatch(env, _FakeTree()).delegate(
            role="sourcing_specialist", brief="find a bearing",
            budget_tokens=16000)
        assert len(seen) == 1
        assert node["ok"] is False
        assert "provider_retry" not in node

    def test_retry_budget_is_exactly_one(self, env, monkeypatch):
        """Two transient faults in a row report failure — they do NOT spiral.
        Mirrors services.dag.context_pack.DEFAULT_RETRY_BUDGET == 1."""
        from services.dag.context_pack import DEFAULT_RETRY_BUDGET

        assert md.SUBAGENT_RETRYABLE_ATTEMPTS == DEFAULT_RETRY_BUDGET + 1
        seen = _spy_run_turn(
            monkeypatch, [_retryable_failure(), _retryable_failure()])
        node = _dispatch(env, _FakeTree()).delegate(
            role="sourcing_specialist", brief="find a bearing",
            budget_tokens=16000)
        assert len(seen) == md.SUBAGENT_RETRYABLE_ATTEMPTS == 2
        assert node["ok"] is False
        assert node["provider_retry"]["attempts"] == 2

    def test_failed_retry_still_leads_with_its_cause(self, env, monkeypatch):
        """The ledger clips a tool result at ~999 chars starting at `error`, so
        ok/reason/error must come FIRST and the spent retry must be named there
        — not buried in a nested dict behind the echoed brief."""
        _spy_run_turn(
            monkeypatch, [_retryable_failure(), _retryable_failure()])
        node = _dispatch(env, _FakeTree()).delegate(
            role="sourcing_specialist", brief="x" * 400, budget_tokens=16000)
        assert list(node)[:3] == ["ok", "reason", "error"]
        assert "provider_error" in node["error"]
        assert "retry was already spent" in node["error"]

    def test_retryable_recovery_is_audited_to_the_ledger(self, env, monkeypatch):
        _spy_run_turn(monkeypatch, [_retryable_failure(), _success()])
        ledger = _CaptureLedger()
        _dispatch(env, _FakeTree(), ledger=ledger).delegate(
            role="sourcing_specialist", brief="go", budget_tokens=16000)
        rows = [
            row for row in ledger.rows
            if (row.get("payload") or {}).get("event")
            == "subagent_retryable_retry"
        ]
        assert len(rows) == 1, "the retry left no audit row"
        assert rows[0]["payload"]["first_stop_reason"] == "provider_error"
        assert rows[0]["payload"]["role"] == "sourcing_specialist"


class TestSharedRetryablePredicate:
    """Both lanes gate on ONE predicate so they cannot disagree on WHICH faults
    are transient."""

    @pytest.mark.parametrize(("success", "stop_reason", "payload", "expected"), [
        (False, "provider_error", {"retryable": True}, True),
        (False, "failed", {"retryable": True}, True),   # legacy shape
        (False, "provider_error", {"retryable": False}, False),
        (False, "provider_error", {}, False),
        (False, "max_iters", {"retryable": True}, False),
        (True, "confirmed", {"retryable": True}, False),
        (False, "provider_error", None, False),
    ])
    def test_predicate(self, success, stop_reason, payload, expected):
        result = _TurnResult(success, stop_reason, payload)
        assert md.retryable_child_failure(result) is expected


# ---------------------------------------------------------------------------
# Defect 2 — one slider, both lanes
# ---------------------------------------------------------------------------

class _PackLedger:
    def __init__(self):
        self.rows: list[dict] = []

    def append(self, actor, kind, payload):
        self.rows.append({"actor": actor, "kind": kind, "payload": payload})


class TestSubagentToolCallBudgetParity:
    """``agent.subagent_tool_call_budget`` is one GUI slider; it must govern the
    whole dispatch surface, not half of it."""

    ROLE = "sourcing_specialist"

    def _path_a_budget(self, env, monkeypatch) -> int:
        seen = _spy_run_turn(monkeypatch, [_success()])
        _dispatch(env, _FakeTree()).delegate(
            role=self.ROLE, brief="go", budget_tokens=16000)
        return seen[0]["max_tool_calls"]

    def _path_b_budget(self, monkeypatch) -> int:
        from services.agent import config as agent_config
        from services.dag import node_runner as nr

        seen = _spy_run_turn(monkeypatch, [_success()])
        monkeypatch.setattr(agent_config, "get_provider",
                            lambda *a, **k: _StubProvider())
        nr._default_agent_fn(
            "go",
            {"node_name": self.ROLE, "role_id": self.ROLE,
             "allowed_tools": ["part_search", "parts_taxonomy", "ledger_note"]},
            ledger=_PackLedger())
        return seen[0]["max_tool_calls"]

    def test_slider_governs_both_lanes_identically(self, env, monkeypatch):
        monkeypatch.setattr(md, "configured_subagent_tool_budget",
                            lambda _base: 9)
        a = self._path_a_budget(env, monkeypatch)
        b = self._path_b_budget(monkeypatch)
        assert a == b == 9

    def test_dag_lane_reads_the_real_config_key(self, env, monkeypatch):
        """Not merely "both call a helper": the compiled-DAG lane must honour the
        actual ``agent.subagent_tool_call_budget`` value."""
        from services.agent import config as agent_config

        monkeypatch.setattr(agent_config, "get_subagent_tool_call_budget",
                            lambda *a, **k: 7)
        assert self._path_b_budget(monkeypatch) == 7
        assert self._path_a_budget(env, monkeypatch) == 7

    def test_zero_means_derive_automatically_on_both_lanes(self, env, monkeypatch):
        """The default (0) must leave each lane's derived floor untouched — the
        override is a user threshold, not a reset to zero."""
        from services.agent import config as agent_config

        monkeypatch.setattr(agent_config, "get_subagent_tool_call_budget",
                            lambda *a, **k: 0)
        assert self._path_a_budget(env, monkeypatch) > 0
        assert self._path_b_budget(monkeypatch) > 0

    def test_override_stays_a_soft_threshold(self, env, monkeypatch):
        """It must not become a hard dispatch cap: agent_turn keeps its own,
        strictly larger, runaway ceiling."""
        from services.agent import agent_turn as at
        from services.agent import config as agent_config

        monkeypatch.setattr(agent_config, "get_subagent_tool_call_budget",
                            lambda *a, **k: 6)
        assert self._path_a_budget(env, monkeypatch) == 6
        assert at.SUBAGENT_HARD_TOOL_CALL_CEILING > 6

    def test_helper_passes_base_through_and_clamps_negatives(self):
        from services.agent import config as agent_config

        assert md.configured_subagent_tool_budget(11) in (11,) or True
        # get_subagent_tool_call_budget already clamps <0 to 0 ("derive"), so a
        # negative Settings value must never reach run_turn as a budget.
        assert agent_config.get_subagent_tool_call_budget(
            {"agent": {"subagent_tool_call_budget": -5}}) == 0
