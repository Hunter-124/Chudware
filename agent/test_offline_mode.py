"""C35 — offline / local-only mode tests (the ITAR-compliance feature).

Scripts config dicts directly into ``config.get_provider`` (no global config.json dependency) and
checks the refusal/allow matrix. House style: plain ``main()`` runner with asserts; also pytest-
collectable.

Matrix:
  * offline + Anthropic cloud   -> refused (OfflineModeError naming the setting)
  * offline + OpenAI cloud      -> refused
  * offline + localhost endpoint-> allowed
  * offline + remote endpoint   -> refused
  * offline OFF                  -> everything resolves as before
  * the assert_offline_safe guard refuses a non-local provider when the flag is set
"""
from __future__ import annotations

import os

# These tests script config dicts directly and assert provider RESOLUTION, so the env-first
# AI_CAD_PROVIDER override must be cleared (it would short-circuit to mock and mask the matrix).
os.environ.pop("AI_CAD_PROVIDER", None)

from agent import config
from agent.config import OfflineModeError


def _expect_refused(cfg: dict, why: str) -> None:
    try:
        config.get_provider(cfg=cfg)
    except OfflineModeError as e:
        assert config.OFFLINE_SETTING in str(e), f"error must name the setting: {e}"
        return
    raise AssertionError(f"expected OfflineModeError ({why}) but provider resolved")


def test_offline_refuses_cloud_anthropic() -> None:
    _expect_refused({"offline_mode": True, "provider": "anthropic", "api_key": "x"},
                    "anthropic cloud under offline")
    print("[offline] anthropic cloud refused")


def test_offline_refuses_cloud_openai() -> None:
    # OpenAI with NO base_url = the real cloud endpoint -> refused.
    _expect_refused({"offline_mode": True, "provider": "openai", "api_key": "x"},
                    "openai cloud under offline")
    print("[offline] openai cloud refused")


def test_offline_refuses_remote_endpoint() -> None:
    _expect_refused({"offline_mode": True, "provider": "openai",
                     "openai": {"base_url": "http://some.remote:1234/v1"}},
                    "remote openai-compatible endpoint under offline")
    print("[offline] remote endpoint refused")


def test_offline_allows_localhost() -> None:
    # localhost / 127.0.0.1 / ::1 are the only allowed hosts.
    for host in ("http://localhost:1234/v1", "http://127.0.0.1:5000/v1", "http://[::1]:8080/v1"):
        prov = config.get_provider(cfg={"offline_mode": True, "provider": "openai",
                                        "openai": {"base_url": host}})
        assert config.provider_offline_safe(prov), f"{host} should be offline-safe"
    print("[offline] localhost/127.0.0.1/::1 allowed")


def test_offline_allows_default_local() -> None:
    # No provider name + offline -> the default local LM Studio (loopback) is allowed.
    prov = config.get_provider(cfg={"offline_mode": True})
    assert config.provider_offline_safe(prov), "default local provider must be offline-safe"
    print("[offline] default local provider allowed")


def test_offline_off_allows_everything() -> None:
    # Flag off: cloud providers resolve normally (construction may need SDKs; we only check the
    # offline guard does NOT block — so use the mock provider name to avoid SDK/key needs).
    prov = config.get_provider(name="mock", cfg={"offline_mode": False})
    assert prov is not None, "mock should resolve with offline off"
    # provider_offline_safe(mock) is True regardless; the point is no refusal was raised.
    print("[offline] flag off -> no refusal")


def test_assert_offline_safe_guard() -> None:
    """The defense-in-depth helper refuses a non-local provider only when the flag is set."""
    class FakeCloud:
        name = "anthropic"
        base_url = None

    class FakeLocal:
        name = "openai"
        base_url = "http://localhost:1234/v1"

    # Flag set: cloud refused, local allowed.
    try:
        config.assert_offline_safe(FakeCloud(), cfg={"offline_mode": True})
        raise AssertionError("expected OfflineModeError for cloud under offline")
    except OfflineModeError as e:
        assert config.OFFLINE_SETTING in str(e)
    config.assert_offline_safe(FakeLocal(), cfg={"offline_mode": True})  # no raise
    # Flag off: nothing refused.
    config.assert_offline_safe(FakeCloud(), cfg={"offline_mode": False})
    print("[offline] assert_offline_safe guard correct")


def main() -> None:
    test_offline_refuses_cloud_anthropic()
    test_offline_refuses_cloud_openai()
    test_offline_refuses_remote_endpoint()
    test_offline_allows_localhost()
    test_offline_allows_default_local()
    test_offline_off_allows_everything()
    test_assert_offline_safe_guard()
    print("\nOFFLINE MODE (C35) TESTS PASSED")


if __name__ == "__main__":
    main()
