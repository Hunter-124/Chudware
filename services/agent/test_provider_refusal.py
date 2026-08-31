"""Provider-neutral content-refusal classification regressions."""

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
