"""Offline tests for provider logging and shadow-log reporting."""
from __future__ import annotations

import json
import sys
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
# Stub services.paths and services.agent.config BEFORE importing the modules
# under test, so the lazy helpers inside them can fire without EnvironmentError.
# ---------------------------------------------------------------------------
import types as _types

# services.paths stub (engine/paths.py was re-homed to services/paths.py) —
# import the real module when available and only fill in the paths attribute.
try:
    import services as _services_pkg  # the real package
except ImportError:
    _services_pkg = _types.ModuleType("services")
    sys.modules.setdefault("services", _services_pkg)
if "services.paths" not in sys.modules:
    try:
        import services.paths as _engine_paths  # real module; has ROOT (DATA never existed)
    except ImportError:
        _engine_paths = _types.ModuleType("services.paths")
        sys.modules["services.paths"] = _engine_paths
        _services_pkg.paths = _engine_paths
else:
    _engine_paths = sys.modules["services.paths"]
if not hasattr(_engine_paths, "DATA"):
    _engine_paths.DATA = Path("/tmp/chudware_test_data")  # legacy attr some shadow tests read

# ---------------------------------------------------------------------------
# services.agent.config stub — injected via a session-scoped autouse fixture
# so sys.modules["services.agent.config"] is RESTORED after this module runs.
# Previously this was a bare sys.modules.setdefault() at collection time, which
# caused 5 pollution regressions in engine_oracle tests (RC#1 from an audit):
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

    return stub


@pytest.fixture(autouse=True, scope="module")
def _patch_svc_agent_config():
    """Inject services.agent.config stub for this module only; restore on teardown.

    scope="module" means this runs once per test module, not once per test.
    The explicit save/restore guarantees other test modules (e.g. telemetry_consent,
    spec_sheet_matcher) see the real services.agent.config — fixing the 5 RC#1
    pollution regressions identified in the audit.
    """
    _SENTINEL = object()
    key = "services.agent.config"
    _saved = sys.modules.get(key, _SENTINEL)
    stub = _make_svc_agent_config_stub()
    sys.modules[key] = stub
    # ALSO patch the parent-package attribute: once the REAL config module has been
    # imported anywhere earlier in the session (e.g. the execute_dag path in
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
# Shadow-log report tests
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


