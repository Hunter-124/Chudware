"""Live regression gate: the shared daemon can reach its OMP provider sidecar.

Guards the failure class fixed 2026-08-06: every ``agent_provider_*`` /
``agent_models_list`` RPC is proxied to ``bun run vendor/omp/bridge/src/bridge.ts``
(core/agent/omp_bridge_client.cpp), resolved through ``CHUDWARE_REPO``. That
workspace's ``node_modules`` is gitignored, so pulling the deployment's checkout
forward past a ``bun.lock`` bump left it half-upgraded: bun aborted at import
("SyntaxError: Export named 'vectorIndexTopK' not found in module
.../@oh-my-pi/pi-natives/native/index.js"), the child died before answering
anything, and the web client's Settings -> Providers rendered an empty list under
a bare transport error — nobody could sign a provider in at all.

Same shape as test_agent_prompts.py: a FAST gate (no LLM call, no credentials
touched) so a deploy-provisioning regression surfaces here instead of when someone
tries to link an API key. Env-gated — needs a running daemon (CHUDWARE_LIVE=1).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure the tests/ dir is importable regardless of pytest's import mode so the
# shared HostdClient (live_smoke.py) resolves whether run as a file or via the suite.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_smoke import SOCK, HostdClient  # noqa: E402

_LIVE = os.environ.get("CHUDWARE_LIVE") == "1"

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="set CHUDWARE_LIVE=1 to run against live hostd socket",
)


def _hello(c: HostdClient) -> None:
    h = c.call("hello", {"client": "gui", "pid": 1, "proto": 1})
    assert (h.get("result") or {}).get("state") == "ready", h


def test_daemon_reaches_provider_bridge() -> None:
    """agent_provider_list must answer with a real catalogue, not a transport error."""
    if not os.path.exists(SOCK):
        pytest.skip(f"hostd socket missing: {SOCK}")
    c = HostdClient(SOCK, "providers")
    try:
        _hello(c)
        r = c.call("agent_provider_list", {})
        assert r.get("error") is None, r.get("error")
        res = r.get("result") or {}
        # ok:false carries the bridge child's own stderr tail since the fix, so a
        # rotted node_modules names itself here instead of hiding in the journal.
        assert res.get("ok") is True, res.get("error")
        providers = res.get("providers") or []
        assert providers, "bridge answered with an empty provider catalogue"
        # Every row the Providers tab renders needs an id + an auth affordance;
        # a catalogue of unusable rows is the same dead end as no catalogue.
        assert all(p.get("id") for p in providers), providers[:3]
        assert any(p.get("authKinds") for p in providers), providers[:3]
    finally:
        c.close()


def test_rejected_api_key_is_not_reported_as_valid() -> None:
    """The link-a-key path must reach the provider AND report its verdict.

    Two failure classes in one probe. (1) A dead sidecar: the call never gets to
    OpenAI. (2) The 2026-08-06 bug in omp_bridge_proxy, which stamped its own
    transport-level ``ok: true`` over the handler's payload verdict — Settings ->
    Providers reads exactly that flag, so a rejected key sailed through "Validate
    & add" and was saved as a live credential. A deliberately invalid key: an
    upstream rejection is the pass condition, a claimed success is the bug.
    """
    if not os.path.exists(SOCK):
        pytest.skip(f"hostd socket missing: {SOCK}")
    c = HostdClient(SOCK, "providerkey")
    try:
        _hello(c)
        r = c.call(
            "agent_provider_validate_key",
            {"provider": "openai", "apiKey": "sk-invalid-live-gate-probe"},
        )
        assert r.get("error") is None, r.get("error")
        res = r.get("result") or {}
        assert res.get("ok") is False, f"a junk key must not validate: {res}"
        detail = f"{res.get('detail', '')} {res.get('error', '')}".strip()
        assert detail, res
        # The verdict has to come from the provider, never from a dead sidecar.
        assert "subprocess exited" not in detail.lower(), detail
    finally:
        c.close()
