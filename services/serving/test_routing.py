"""Tests for services/serving/routing.py (T-H9).

Pure Python, no engine / GPU / network. Run:
    python -m pytest services/serving/test_routing.py -q
"""
from __future__ import annotations

import json

import pytest

from services.serving.routing import (
    Fallback,
    RouteDecision,
    RouteEntry,
    RoutingError,
    RoutingTable,
    canary_bucket,
    load_routing_table,
    save_routing_table,
    with_canary_pct,
)


# --------------------------------------------------------------------------- Fallback / kill-switch

def test_fallback_rejects_non_killswitch_mode():
    with pytest.raises(RoutingError):
        Fallback(mode="another_adapter")  # only deterministic|frontier allowed


def test_fallback_modes_accepted():
    assert Fallback(mode="deterministic").mode == "deterministic"
    assert Fallback(mode="frontier", provider="anthropic").provider == "anthropic"


def test_fallback_has_no_adapter_field():
    # Structural kill-switch guarantee: a Fallback cannot carry an adapter_version.
    assert not hasattr(Fallback(), "adapter_version")


def test_route_entry_requires_fallback_in_dict():
    with pytest.raises(RoutingError):
        RouteEntry.from_dict("IP-13", {"provider": "vllm", "model": "base"})  # no fallback


def test_route_entry_rejects_bad_pct():
    with pytest.raises(RoutingError):
        RouteEntry("IP-1", "vllm", "base", canary_pct=150.0, fallback=Fallback())


# --------------------------------------------------------------------------- bucketing determinism

def test_canary_bucket_is_deterministic_and_in_range():
    for uid in ("alice", "bob", "carol", ""):
        b1 = canary_bucket("IP-13", uid, "salt")
        b2 = canary_bucket("IP-13", uid, "salt")
        assert b1 == b2
        assert 0 <= b1 < 100


def test_canary_bucket_varies_by_user_and_salt():
    a = canary_bucket("IP-13", "alice", "s1")
    b = canary_bucket("IP-13", "bob", "s1")
    c = canary_bucket("IP-13", "alice", "s2")
    assert len({a, b, c}) >= 2  # not all identical


def test_canary_pct_admits_expected_fraction():
    # With a fixed salt, ~pct% of a large user population buckets in. Assert it's in the ballpark.
    salt = "IP-13"
    admitted = sum(1 for i in range(2000) if canary_bucket("IP-13", f"user{i}", salt) < 10)
    assert 150 <= admitted <= 250  # ~10% of 2000, generous band


# --------------------------------------------------------------------------- resolve() branches

def _entry(**kw) -> RouteEntry:
    base = dict(
        insertion_point="IP-13",
        provider="vllm",
        model="qwen3-4b",
        adapter_version="IP-13/v3",
        fallback=Fallback(mode="frontier", provider="anthropic", model="claude"),
    )
    base.update(kw)
    return RouteEntry(**base)


def test_no_adapter_configured_falls_back():
    t = RoutingTable([_entry(adapter_version=None)])
    d = t.resolve("IP-13", user_id="alice")
    assert d.is_fallback and d.adapter is None
    assert d.provider == "anthropic"
    assert d.reason.startswith("fallback:no_adapter")


def test_unhealthy_adapter_degrades_without_error():
    t = RoutingTable([_entry(canary_pct=100.0)])
    d = t.resolve("IP-13", user_id="alice", adapter_healthy=False)
    assert d.is_fallback and d.adapter is None
    assert "adapter_unhealthy" in d.reason


def test_opt_in_user_always_gets_adapter():
    t = RoutingTable([_entry(canary_pct=0.0, canary_opt_in=("alice",))])
    d = t.resolve("IP-13", user_id="alice")
    assert not d.is_fallback and d.adapter == "IP-13/v3"
    assert d.is_canary  # opt-in during a 0% rollout is a canary
    assert d.reason == "adapter:opt_in"


def test_non_opt_in_user_excluded_at_zero_pct():
    t = RoutingTable([_entry(canary_pct=0.0)])
    d = t.resolve("IP-13", user_id="nobody")
    assert d.is_fallback and d.adapter is None


def test_full_promotion_is_not_canary():
    t = RoutingTable([_entry(canary_pct=100.0)])
    d = t.resolve("IP-13", user_id="anyone")
    assert not d.is_fallback and d.adapter == "IP-13/v3"
    assert not d.is_canary  # 100% == production, not canary


def test_unknown_insertion_point_raises():
    t = RoutingTable([_entry()])
    with pytest.raises(RoutingError):
        t.resolve("IP-99", user_id="alice")


# --------------------------------------------------------------------------- canary flip 0 -> 10 -> 0

def test_canary_flip_live_zero_ten_zero():
    """DoD: canary 0%->10%->0% changes who is admitted, deterministically, no restart."""
    table = RoutingTable([_entry(canary_pct=0.0)])
    users = [f"user{i}" for i in range(1000)]

    def admitted(pct):
        table.put(with_canary_pct(table.get("IP-13"), pct))
        return {u for u in users if not table.resolve("IP-13", user_id=u).is_fallback}

    at0 = admitted(0.0)
    at10 = admitted(10.0)
    back0 = admitted(0.0)

    assert at0 == set()            # nobody at 0%
    assert 50 <= len(at10) <= 200  # ~10% admitted
    assert back0 == set()          # rollback to 0% removes everyone
    # Admission is a stable subset: everyone admitted at 10% was NOT admitted at 0% (trivially),
    # and re-flipping to 10% would re-admit exactly the same set (determinism).
    table.put(with_canary_pct(table.get("IP-13"), 10.0))
    reflip = {u for u in users if not table.resolve("IP-13", user_id=u).is_fallback}
    assert reflip == at10


def test_every_canary_response_is_tagged():
    """DoD: every canary response carries the adapter in its telemetry model tag."""
    t = RoutingTable([_entry(canary_pct=100.0, canary_opt_in=("early",))])
    d_canary = t.resolve("IP-13", user_id="early")  # opt-in => canary even at 100%
    assert d_canary.is_canary
    tag = d_canary.telemetry_tag()
    assert tag == {"provider": "vllm", "model": "qwen3-4b", "adapter": "IP-13/v3"}

    # fallback response is NOT tagged with an adapter
    t2 = RoutingTable([_entry(adapter_version=None)])
    d_fb = t2.resolve("IP-13", user_id="x")
    assert d_fb.telemetry_tag()["adapter"] is None


# --------------------------------------------------------------------------- serialization

def test_round_trip_dict():
    e = _entry(canary_pct=25.0, canary_opt_in=("a", "b"), history=("IP-13/v1", "IP-13/v2"))
    t = RoutingTable([e])
    t2 = RoutingTable.from_dict(t.to_dict())
    e2 = t2.get("IP-13")
    assert e2.adapter_version == "IP-13/v3"
    assert e2.canary_pct == 25.0
    assert e2.canary_opt_in == ("a", "b")
    assert e2.history == ("IP-13/v1", "IP-13/v2")
    assert e2.fallback.mode == "frontier"


def test_load_save_preserves_other_config_keys(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"provider": "anthropic", "model": "claude", "api_key": "sk"}),
                   encoding="utf-8")
    t = RoutingTable([_entry(canary_pct=10.0)])
    save_routing_table(t, cfg_path=cfg)
    written = json.loads(cfg.read_text(encoding="utf-8"))
    # routing subtree added; other keys untouched
    assert written["provider"] == "anthropic"
    assert written["api_key"] == "sk"
    assert "routing" in written and "IP-13" in written["routing"]
    # and it re-loads
    reloaded = load_routing_table(written)
    assert reloaded.get("IP-13").canary_pct == 10.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
