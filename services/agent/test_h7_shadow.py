"""Tests for T-H7 shadow-mode modules.

Covers:
  - services/agent/providers/logging_provider.py  (LoggingProvider)
  - services/agent/shadow.py                      (ShadowExecutor)
  - tools/flywheel/shadow_report.py               (generate_report, load_shadow_log)

No network, no subprocess; all providers are mocks.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

# ---------------------------------------------------------------------------
# Make the worktree root importable.  The test file lives at
#   <root>/services/agent/test_h7_shadow.py  →  parents[2] == <root>
# We also need tools/ importable for shadow_report.
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ---------------------------------------------------------------------------
# Stub engine.paths and services.agent.config BEFORE importing the modules
# under test, so the lazy helpers inside them can fire without EnvironmentError.
# ---------------------------------------------------------------------------
import types as _types

# engine.paths stub — the old bare-ModuleType("engine") injection was NOT harmless:
# it replaced the real `engine` package in sys.modules with a non-package, breaking
# `engine.piping` (etc.) imports in any test module collected AFTER this one
# ("'engine' is not a package" — the last big cross-dir pollution root, 2026-07-04).
# Import the real package when available and only fill in the paths attribute.
try:
    import engine as _engine_pkg  # the real package (repo-root symlink -> engine_oracle)
except ImportError:
    _engine_pkg = _types.ModuleType("engine")
    sys.modules.setdefault("engine", _engine_pkg)
if "engine.paths" not in sys.modules:
    try:
        import engine.paths as _engine_paths  # real module; has ROOT (DATA never existed)
    except ImportError:
        _engine_paths = _types.ModuleType("engine.paths")
        sys.modules["engine.paths"] = _engine_paths
        _engine_pkg.paths = _engine_paths
else:
    _engine_paths = sys.modules["engine.paths"]
if not hasattr(_engine_paths, "DATA"):
    _engine_paths.DATA = Path("/tmp/ai_cad_test_data")  # legacy attr some shadow tests read

# ---------------------------------------------------------------------------
# services.agent.config stub — injected via a session-scoped autouse fixture
# so sys.modules["services.agent.config"] is RESTORED after this module runs.
# Previously this was a bare sys.modules.setdefault() at collection time, which
# caused 5 pollution regressions in engine_oracle tests (RC#1 from T-V2 audit):
#   - consent_tier() always returned "off" → broke 3 telemetry-consent tests
#   - missing get_provider attr → AttributeError in spec_sheet_matcher tests
# ---------------------------------------------------------------------------

def _make_svc_agent_config_stub() -> _types.ModuleType:
    """Build the services.agent.config stub module used by this test file."""
    stub = _types.ModuleType("services.agent.config")
    stub.consent_tier = lambda cfg: "off"       # type: ignore[attr-defined]
    stub.load_config = lambda: {}               # type: ignore[attr-defined]
    stub.offline_enabled = lambda: False        # type: ignore[attr-defined]
    stub.get_provider = lambda name=None: None  # type: ignore[attr-defined]  # prevents attr cascade

    def _assert_offline_safe(provider):  # type: ignore[no-redef]
        if not getattr(provider, "offline_safe", False):
            raise RuntimeError(
                f"assert_offline_safe: provider {getattr(provider, 'name', '?')!r} "
                "is not offline-safe"
            )
    stub.assert_offline_safe = _assert_offline_safe  # type: ignore[attr-defined]
    return stub


@pytest.fixture(autouse=True, scope="module")
def _patch_svc_agent_config():
    """Inject services.agent.config stub for this module only; restore on teardown.

    scope="module" means this runs once per test module, not once per test.
    The explicit save/restore guarantees other test modules (e.g. telemetry_consent,
    spec_sheet_matcher) see the real services.agent.config — fixing the 5 RC#1
    pollution regressions identified in the T-V2 audit.
    """
    _SENTINEL = object()
    key = "services.agent.config"
    _saved = sys.modules.get(key, _SENTINEL)
    stub = _make_svc_agent_config_stub()
    sys.modules[key] = stub
    # ALSO patch the parent-package attribute: once the REAL config module has been
    # imported anywhere earlier in the session (e.g. the T-W30 execute_dag path in
    # pipeline tests imports it for provider resolution), ``from services.agent
    # import config`` resolves the *package attribute*, not sys.modules — so the
    # sys.modules stub alone silently stops taking effect (order-dependent flake).
    _pkg = sys.modules.get("services.agent")
    _saved_attr = getattr(_pkg, "config", _SENTINEL) if _pkg is not None else _SENTINEL
    if _pkg is not None:
        _pkg.config = stub  # type: ignore[attr-defined]
    yield
    if _saved is _SENTINEL:
        sys.modules.pop(key, None)
    else:
        sys.modules[key] = _saved  # type: ignore[assignment]
    if _pkg is not None:
        if _saved_attr is _SENTINEL:
            if getattr(_pkg, "config", None) is stub:
                del _pkg.config  # type: ignore[attr-defined]
        else:
            _pkg.config = _saved_attr  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Now it is safe to import the modules under test.
# ---------------------------------------------------------------------------
from services.agent.providers.logging_provider import (  # noqa: E402
    LoggingProvider,
    get_last_sidecar_paths,
    wrap,
)
from services.agent.shadow import ShadowExecutor, ShadowRecord  # noqa: E402
from tools.flywheel.shadow_report import (  # noqa: E402
    generate_fixture_log,
    generate_report,
    load_shadow_log,
    ShadowRow,
)


# ===========================================================================
# Shared fixtures and helpers
# ===========================================================================

class MockProvider:
    """Instant, offline, no-network provider for tests."""

    def __init__(
        self,
        name: str = "mock",
        model: str = "mock-v1",
        offline_safe: bool = True,
        supports_vision: bool = False,
        base_url: str | None = "http://localhost:9999",
        reply: str = "MOCK_REPLY",
        thinking: str = "MOCK_THINK",
    ):
        self.name = name
        self.model = model
        self.offline_safe = offline_safe
        self.supports_vision = supports_vision
        self.base_url = base_url
        self._reply = reply
        self._thinking = thinking

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        return self._reply

    def complete_ex(
        self, system: str, messages: list[dict], max_tokens: int = 8000
    ) -> tuple[str, str]:
        return self._reply, self._thinking


def _simple_messages(text: str = "hello") -> list[dict]:
    return [{"role": "user", "content": [{"type": "text", "text": text}]}]


def _make_lp(
    tmp_path: Path,
    *,
    consent: str = "off",
    suppress_emit: bool = True,
    on_event: Callable[[dict], None] | None = None,
    inner: MockProvider | None = None,
) -> LoggingProvider:
    """Convenience factory — always injects sidecar_dir and consent_fn."""
    return LoggingProvider(
        inner=inner or MockProvider(),
        on_event=on_event,
        sidecar_dir=tmp_path / "sidecars",
        suppress_emit=suppress_emit,
        consent_fn=lambda: consent,
        run_id="testrun",
    )


# ===========================================================================
# 3. LoggingProvider tests
# ===========================================================================


class TestLoggingProviderDelegation:
    """3a — complete_ex delegates and passes through text+thinking."""

    def test_returns_inner_text_and_thinking(self, tmp_path: Path) -> None:
        inner = MockProvider(reply="HELLO", thinking="THINK")
        lp = _make_lp(tmp_path, inner=inner)
        text, thinking = lp.complete_ex("sys", _simple_messages())
        assert text == "HELLO"
        assert thinking == "THINK"

    def test_complete_returns_only_text(self, tmp_path: Path) -> None:
        inner = MockProvider(reply="ONLY_TEXT")
        lp = _make_lp(tmp_path, inner=inner)
        result = lp.complete("sys", _simple_messages())
        assert result == "ONLY_TEXT"


class TestLoggingProviderProxy:
    """3b — attributes are proxied from the inner provider."""

    def test_proxy_name(self, tmp_path: Path) -> None:
        lp = _make_lp(tmp_path, inner=MockProvider(name="mock"))
        assert lp.name == "mock"

    def test_proxy_model(self, tmp_path: Path) -> None:
        lp = _make_lp(tmp_path, inner=MockProvider(model="mock-v1"))
        assert lp.model == "mock-v1"

    def test_proxy_supports_vision(self, tmp_path: Path) -> None:
        lp = _make_lp(tmp_path, inner=MockProvider(supports_vision=False))
        assert lp.supports_vision is False

    def test_proxy_offline_safe(self, tmp_path: Path) -> None:
        lp = _make_lp(tmp_path, inner=MockProvider(offline_safe=True))
        assert lp.offline_safe is True

    def test_proxy_base_url(self, tmp_path: Path) -> None:
        lp = _make_lp(tmp_path, inner=MockProvider(base_url="http://local:8000"))
        assert lp.base_url == "http://local:8000"


class TestLoggingProviderSidecars:
    """3c–3d — sidecars are written only at full consent."""

    def test_full_consent_writes_sidecars(self, tmp_path: Path) -> None:
        lp = _make_lp(tmp_path, consent="full")
        lp.complete_ex("sys", _simple_messages())
        paths = get_last_sidecar_paths()
        assert paths["input_ref"], "input_ref should be non-empty string"
        assert paths["output_ref"], "output_ref should be non-empty string"
        assert Path(paths["input_ref"]).exists()
        assert Path(paths["output_ref"]).exists()

    def test_full_consent_sidecar_content_non_empty(self, tmp_path: Path) -> None:
        lp = _make_lp(tmp_path, consent="full")
        lp.complete_ex("sys", _simple_messages("test content"))
        paths = get_last_sidecar_paths()
        assert Path(paths["input_ref"]).stat().st_size > 0
        assert Path(paths["output_ref"]).stat().st_size > 0

    @pytest.mark.parametrize("consent", ["off", "meta"])
    def test_non_full_consent_no_sidecars(self, tmp_path: Path, consent: str) -> None:
        lp = _make_lp(tmp_path, consent=consent)
        lp.complete_ex("sys", _simple_messages())
        paths = get_last_sidecar_paths()
        assert paths["input_ref"] == "", f"input_ref should be empty for consent={consent!r}"
        assert paths["output_ref"] == "", f"output_ref should be empty for consent={consent!r}"


class TestLoggingProviderGetLastPaths:
    """3f — get_last_sidecar_paths returns the written paths after full-consent call."""

    def test_returns_paths_dict(self, tmp_path: Path) -> None:
        lp = _make_lp(tmp_path, consent="full")
        lp.complete_ex("sys", _simple_messages())
        result = get_last_sidecar_paths()
        assert isinstance(result, dict)
        assert "input_ref" in result
        assert "output_ref" in result

    def test_paths_are_strings(self, tmp_path: Path) -> None:
        lp = _make_lp(tmp_path, consent="full")
        lp.complete_ex("sys", _simple_messages())
        result = get_last_sidecar_paths()
        assert isinstance(result["input_ref"], str)
        assert isinstance(result["output_ref"], str)


class TestLoggingProviderEmit:
    """3g–3h — suppress_emit controls whether on_event is called."""

    def test_suppress_emit_false_calls_on_event(self, tmp_path: Path) -> None:
        events: list[dict] = []
        lp = _make_lp(tmp_path, suppress_emit=False, on_event=events.append, consent="full")
        lp.complete_ex("sys", _simple_messages())
        assert len(events) == 1
        assert events[0]["kind"] == "ai_artifact"

    def test_suppress_emit_true_no_on_event(self, tmp_path: Path) -> None:
        events: list[dict] = []
        lp = _make_lp(tmp_path, suppress_emit=True, on_event=events.append, consent="full")
        lp.complete_ex("sys", _simple_messages())
        assert events == []

    def test_suppress_emit_false_consent_off_still_calls_event(self, tmp_path: Path) -> None:
        """Even with consent=off the event is emitted (just with empty refs)."""
        events: list[dict] = []
        lp = _make_lp(tmp_path, suppress_emit=False, on_event=events.append, consent="off")
        lp.complete_ex("sys", _simple_messages())
        assert len(events) == 1


class TestLoggingProviderOverhead:
    """3i — 1000 calls with suppress_emit=True, consent=off completes in <10s."""

    def test_overhead_1000_calls(self, tmp_path: Path) -> None:
        lp = _make_lp(tmp_path, suppress_emit=True, consent="off")
        msgs = _simple_messages("benchmark prompt")
        t0 = time.monotonic()
        for _ in range(1000):
            lp.complete_ex("sys", msgs)
        elapsed = time.monotonic() - t0
        assert elapsed < 10.0, f"1000 calls took {elapsed:.2f}s (expected <10s)"


class TestLoggingProviderProxyOfflineSafe:
    """3j — LoggingProvider wrapping MockProvider proxies name and offline_safe."""

    def test_name_and_offline_safe_proxied(self, tmp_path: Path) -> None:
        inner = MockProvider(name="mock", offline_safe=True)
        lp = _make_lp(tmp_path, inner=inner)
        assert lp.name == "mock"
        assert lp.offline_safe is True


# ===========================================================================
# 4. ShadowExecutor tests
# ===========================================================================

def _make_ai_artifact_events_jsonl(path: Path, n: int = 5) -> None:
    """Write n synthetic ai_artifact event lines (the schema replay_log expects)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(n):
            event = {
                "kind": "ai_artifact",
                "artifact_id": f"{i:08x}",
                "insertion_point": "IP-1",
                "model": {"provider": "mock", "model": "mock-v1"},
                "prompt_hash": f"{i:016x}",
                "input_ref": "",   # no sidecar; executor synthesises minimal request
                "output_ref": "",
                "latency_ms": 500.0 + i,
                "verifier_scores": {},
                "consent": "full",
            }
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


class TestShadowExecutorReplay:
    """4a–4b — replay_log over ai_artifact events returns correct records."""

    def test_replay_returns_n_records(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _make_ai_artifact_events_jsonl(events_path, n=5)
        challenger = MockProvider(name="mock-challenger", model="mock-v2")
        executor = ShadowExecutor(challenger, offline_mode=False)
        records = executor.replay_log(events_path)
        assert len(records) == 5

    def test_replay_records_are_shadow_records(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _make_ai_artifact_events_jsonl(events_path, n=5)
        challenger = MockProvider(name="mock-challenger", model="mock-v2")
        executor = ShadowExecutor(challenger, offline_mode=False)
        records = executor.replay_log(events_path)
        assert all(isinstance(r, ShadowRecord) for r in records)

    def test_replay_challenger_provider_name(self, tmp_path: Path) -> None:
        """4b — each record's challenger_provider equals the challenger's name."""
        events_path = tmp_path / "events.jsonl"
        _make_ai_artifact_events_jsonl(events_path, n=5)
        challenger = MockProvider(name="mock-challenger", model="mock-v2")
        executor = ShadowExecutor(challenger, offline_mode=False)
        records = executor.replay_log(events_path)
        for rec in records:
            assert rec.challenger_provider == "mock-challenger"


class TestShadowExecutorOfflineMode:
    """4c — offline_mode=True with a non-offline-safe cloud provider raises."""

    def test_offline_mode_refuses_cloud_provider(self) -> None:
        cloud = MockProvider(name="anthropic", offline_safe=False)
        with pytest.raises((RuntimeError, Exception)) as exc_info:
            ShadowExecutor(cloud, offline_mode=True)
        # Ensure it's the right kind of error
        assert "offline" in str(exc_info.value).lower() or "anthropic" in str(exc_info.value).lower()

    def test_offline_mode_allows_offline_safe_provider(self) -> None:
        safe = MockProvider(name="ollama", offline_safe=True)
        # Should not raise
        executor = ShadowExecutor(safe, offline_mode=True)
        assert executor is not None


class TestShadowExecutorRunParallel:
    """4d–4e — run_parallel queues tasks, drain() delivers on_record callbacks."""

    def _make_instant_rate_limiter(self):
        """_RateLimiter with very high calls/min so no sleep delay."""
        from services.agent.shadow import _RateLimiter
        return _RateLimiter(calls_per_minute=100_000)

    def test_drain_delivers_on_record(self, tmp_path: Path) -> None:
        received: list[ShadowRecord] = []
        challenger = MockProvider(name="mock")
        executor = ShadowExecutor(
            challenger,
            on_record=received.append,
            rate_limiter=self._make_instant_rate_limiter(),
            offline_mode=False,
        )
        executor.run_parallel(
            "INCUMBENT_TEXT",
            "system",
            _simple_messages(),
            artifact_id="test0001",
            insertion_point="IP-1",
            incumbent_provider="mock",
            incumbent_model="mock-v1",
            incumbent_latency_ms=100.0,
        )
        executor.drain(timeout=10.0)
        assert len(received) == 1
        assert isinstance(received[0], ShadowRecord)

    def test_full_queue_does_not_block_or_raise(self) -> None:
        """4e — run_parallel drops (not blocks) when the queue is full."""
        # Block the background worker so the queue stays full.
        blocker = threading.Event()
        calls: list[int] = []

        class BlockingProvider(MockProvider):
            def complete_ex(self, system, messages, max_tokens=8000):
                blocker.wait()  # stall the worker
                calls.append(1)
                return super().complete_ex(system, messages, max_tokens)

        challenger = BlockingProvider(name="mock")
        from services.agent.shadow import _RateLimiter
        executor = ShadowExecutor(
            challenger,
            rate_limiter=_RateLimiter(calls_per_minute=100_000),
            offline_mode=False,
        )

        # Enqueue more than maxsize=50 — should never block or raise.
        t0 = time.monotonic()
        for i in range(60):
            executor.run_parallel(
                "text",
                "sys",
                _simple_messages(),
                artifact_id=f"art{i:04d}",
                insertion_point="IP-1",
                incumbent_provider="mock",
                incumbent_model="mock-v1",
                incumbent_latency_ms=10.0,
            )
        elapsed = time.monotonic() - t0
        # Unblock so the daemon thread can exit cleanly.
        blocker.set()
        assert elapsed < 5.0, f"run_parallel with full queue blocked for {elapsed:.2f}s"


class TestShadowExecutorConstruction:
    """4f — offline_mode=False with mock provider succeeds."""

    def test_offline_false_mock_challenger_ok(self) -> None:
        mock = MockProvider(name="mock", offline_safe=True)
        executor = ShadowExecutor(mock, offline_mode=False)
        assert executor is not None


# ===========================================================================
# 5. shadow_report.py tests
# ===========================================================================


class TestLoadShadowLog:
    """5a, 5f — load_shadow_log reads rows and tolerates bad lines."""

    def test_load_returns_120_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "shadow.jsonl"
        generate_fixture_log(path, n=120)
        rows = load_shadow_log(path)
        assert len(rows) == 120

    def test_rows_are_shadow_row_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "shadow.jsonl"
        generate_fixture_log(path, n=10)
        rows = load_shadow_log(path)
        assert all(isinstance(r, ShadowRow) for r in rows)

    def test_tolerates_malformed_jsonl(self, tmp_path: Path) -> None:
        """5f — bad JSON lines are skipped; valid lines still returned."""
        path = tmp_path / "bad_shadow.jsonl"
        generate_fixture_log(path, n=5)
        # Inject a malformed line in the middle.
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)
        lines.insert(2, "{not valid json!!!\n")
        path.write_text("".join(lines), encoding="utf-8")
        rows = load_shadow_log(path)
        # 5 valid lines remain (the bad line is skipped)
        assert len(rows) == 5

    def test_tolerates_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        rows = load_shadow_log(path)
        assert rows == []


class TestGenerateReport:
    """5b–5e — generate_report produces correct output."""

    @pytest.fixture()
    def rows_120(self, tmp_path: Path) -> list[ShadowRow]:
        path = tmp_path / "fixture.jsonl"
        generate_fixture_log(path, n=120)
        return load_shadow_log(path)

    @pytest.fixture()
    def rows_50(self, tmp_path: Path) -> list[ShadowRow]:
        path = tmp_path / "fixture50.jsonl"
        generate_fixture_log(path, n=50)
        return load_shadow_log(path)

    def test_report_contains_incumbent_and_challenger(self, rows_120: list[ShadowRow]) -> None:
        """5b — report contains 'Incumbent', 'Challenger', 'N = 120'."""
        report = generate_report(rows_120)
        assert "Incumbent" in report
        assert "Challenger" in report
        assert "N = 120" in report

    def test_report_min_calls_above_count_no_warning(self, rows_120: list[ShadowRow]) -> None:
        """5c — min_calls=100 with 120 rows → no warning."""
        report = generate_report(rows_120, min_calls=100)
        assert "[WARNING]" not in report

    def test_report_min_calls_below_count_shows_warning(self, rows_50: list[ShadowRow]) -> None:
        """5d — min_calls=100 with 50 rows → WARNING line present."""
        report = generate_report(rows_50, min_calls=100)
        assert "[WARNING]" in report

    def test_report_empty_rows_says_no_shadow_records(self) -> None:
        """5e — empty list → something containing 'No shadow records'."""
        report = generate_report([])
        assert "No shadow records" in report

    def test_report_is_string(self, rows_120: list[ShadowRow]) -> None:
        assert isinstance(generate_report(rows_120), str)


# ---------------------------------------------------------------------------
# End-to-end DoD#2: replay >=100 logged calls -> comparison report
# ---------------------------------------------------------------------------

class TestEndToEndReplayReport:
    """DoD#2 — shadow replay over >=100 logged calls produces a comparison report."""

    def test_replay_120_calls_to_report(self, tmp_path: Path) -> None:
        # 1. Log: 120 ai_artifact events, each with a real request sidecar.
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        events_path = tmp_path / "events.jsonl"
        with open(events_path, "w", encoding="utf-8") as fh:
            for i in range(120):
                req_p = sidecar_dir / f"replay_{i:04d}_req.jsonl"
                req_p.write_text(json.dumps({
                    "system": "you are a CAD copilot",
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": f"design part {i}"}]}],
                }) + "\n", encoding="utf-8")
                fh.write(json.dumps({
                    "kind": "ai_artifact",
                    "artifact_id": f"e2e{i:05x}",
                    "insertion_point": "IP-1" if i % 3 else "IP-12",
                    "model": {"provider": "anthropic", "model": "claude-opus-4-8"},
                    "prompt_hash": f"{i:016x}",
                    "input_ref": str(req_p),
                    "output_ref": "",
                    "latency_ms": 1000.0 + i,
                    "verifier_scores": {"verifier": 0.8},
                    "consent": "full",
                }) + "\n")

        # 2. Replay against a mock challenger, appending to a shadow log.
        shadow_log = tmp_path / "shadow_log.jsonl"

        class _Scorer:
            def score(self, output_text, *, context=None):
                return {"verifier": 0.7 if output_text else 0.0}

        ex = ShadowExecutor(
            MockProvider(name="mock", model="challenger-v2", offline_safe=True),
            scorer=_Scorer(),
            offline_mode=False,
            shadow_log_path=shadow_log,
        )
        records = ex.replay_log(events_path)
        assert len(records) == 120

        # 3. Report from the shadow log.
        rows = load_shadow_log(shadow_log)
        assert len(rows) == 120
        report = generate_report(rows, min_calls=100)
        assert "N = 120" in report
        assert "[WARNING]" not in report
        assert "Incumbent" in report and "Challenger" in report
        assert "claude-opus-4-8" in report
        assert "challenger-v2" in report
        assert "verifier" in report            # score table present
        assert "BY INSERTION POINT" in report  # IP-1 + IP-12 breakdown
        assert "Latency delta" in report
        assert "Accept-rate delta" in report
