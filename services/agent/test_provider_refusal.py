"""Provider-neutral content-refusal classification and honest provider-failure regressions."""

from __future__ import annotations

from types import SimpleNamespace

from services.agent import agent_turn
from services.agent.providers.base import ProviderUnreachable


class _ProviderError(RuntimeError):
    def __init__(self, message: str, *, body=None, status: int | None = None):
        super().__init__(message)
        self.body = body
        self.status = status
        self.provider = "anthropic"


def test_structured_stop_details_classifies_content_refusal_before_http_400():
    exc = _ProviderError(
        "request blocked",
        body={"stopDetails": {"type": "refusal", "category": "bio"}},
        status=400,
    )

    assert agent_turn.classify_provider_error(exc) == ("provider_refused", False)
    detail = agent_turn.format_provider_error_detail(
        exc, SimpleNamespace(name="fallback-provider")
    )
    assert "anthropic declined this request on content policy" in detail
    assert "category: bio" in detail
    assert "credential and the connection were all fine" in detail
    assert "_ProviderError" not in detail


def test_nested_provider_code_classifies_uncategorised_refusal():
    exc = _ProviderError(
        "bad request",
        body={
            "error": {
                "code": "content_policy_violation",
                "message": "content flagged by safety filters",
            }
        },
        status=400,
    )

    assert agent_turn.classify_provider_error(exc) == ("provider_refused", False)
    detail = agent_turn.format_provider_error_detail(exc)
    assert "content policy" in detail
    assert "category:" not in detail


def test_explicit_provider_neutral_refusal_flag_is_supported():
    exc = RuntimeError("vendor declined")
    exc.is_content_refusal = True  # type: ignore[attr-defined]
    exc.category = "regulated_goods"  # type: ignore[attr-defined]

    assert agent_turn.classify_provider_error(exc) == ("provider_refused", False)
    assert "category: regulated_goods" in agent_turn.format_provider_error_detail(exc)


def test_loopback_connection_refused_stays_retryable_transport_failure():
    exc = ProviderUnreachable(
        "Could not reach http://127.0.0.1:1234/v1: connection refused"
    )

    reason, retryable = agent_turn.classify_provider_error(exc)
    assert reason != "provider_refused"
    assert retryable is True
    detail = agent_turn.format_provider_error_detail(
        exc, SimpleNamespace(name="llamacpp")
    )
    assert "llamacpp" in detail
    assert "connection refused" in detail


def test_non_refusal_bad_request_is_not_reclassified_by_generic_body_walk():
    exc = _ProviderError(
        "invalid_request_error: empty input",
        body={"error": {"type": "invalid_request_error", "message": "empty input"}},
        status=400,
    )

    assert agent_turn.classify_provider_error(exc) == ("provider_error", False)


def _run_failing_turn(tmp_path, monkeypatch, provider):
    """Run one turn on *provider* with a cloud key configured and record any
    attempt to build a different provider."""
    from services.agent import config as agent_config
    from services.agent.ledger import Ledger

    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-a-credential")
    cfg = {
        "provider": provider.name,
        "model": provider.model,
        "openrouter": {"api_key": "sk-or-test-not-a-credential"},
        "enabled_providers": [
            {"provider": "openrouter", "model": "deepseek/deepseek-chat-v3.1"},
            {"provider": "openai", "model": "gpt-5"},
        ],
    }
    monkeypatch.setattr(agent_config, "load_config", lambda: dict(cfg))
    built: list[dict] = []

    def _no_other_provider(*args, **kwargs):
        built.append({"args": args, "kwargs": kwargs})
        raise AssertionError("run_turn must not build another provider")

    monkeypatch.setattr(agent_config, "get_provider", _no_other_provider)
    ledger = Ledger("provider_failure", path=tmp_path / "ledger.json")
    res = agent_turn.run_turn(
        "finish", provider, name="n_test", ledger=ledger, max_tool_calls=2
    )
    events = [(e.get("payload") or {}).get("event") for e in ledger.events]
    return res, built, events


def test_local_provider_failure_reports_error_without_cloud_fallback(
    tmp_path, monkeypatch
):
    calls: list[int] = []

    class _DeadLocal:
        name = "llamacpp"
        model = "local"

        def complete(self, system, messages, max_tokens=8000):
            calls.append(1)
            raise ProviderUnreachable(
                "Could not reach http://127.0.0.1:1234/v1: connection refused"
            )

    res, built, events = _run_failing_turn(tmp_path, monkeypatch, _DeadLocal())

    assert res.success is False
    assert built == []
    assert len(calls) == 1
    assert "connection refused" in res.result["error"]
    assert res.result["retryable"] is True
    assert "provider_error" in events


def test_content_refusal_stops_without_resending_to_another_vendor(
    tmp_path, monkeypatch
):
    class _Refusing:
        name = "anthropic"
        model = "claude-sonnet-5"

        def complete(self, system, messages, max_tokens=8000):
            exc = RuntimeError("vendor declined")
            exc.is_content_refusal = True  # type: ignore[attr-defined]
            exc.provider = "anthropic"  # type: ignore[attr-defined]
            raise exc

    res, built, events = _run_failing_turn(tmp_path, monkeypatch, _Refusing())

    assert res.success is False
    assert res.stop_reason == "provider_refused"
    assert built == []
    assert "vendor declined" in res.result["error"]
    assert "provider_error" in events
