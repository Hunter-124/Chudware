"""The subscription_* family is operator-global, so the web bridge admin-gates it.

Guards the hole that existed until now: the per-frame filter in engine_bridge.py
rejected ``agent_admin_turn`` and admin-gated ``agent_provider_*``, but nothing
matched ``subscription_*`` — so any authenticated tenant's frame went straight to
the shared daemon's socket. Those RPCs are backed by the ONE chudware-hostd
process and its single process-wide sub_auth credential store, which makes
``subscription_login`` a write to the operator's own Claude/Codex/Cursor/xAI/Kimi
credential (and a webbrowser.open() plus a loopback callback bind on the gateway
host), and makes ``subscription_status``/``_accounts``/``_usage`` a read of the
operator's account emails and subscription usage that ``providers.py`` withholds
from non-admins via ``include_operator_detail``.

Reuses test_engine_bridge.py's hermetic fake-daemon harness: the refusal has to be
proven by the absence of the frame on the daemon socket, not by the error code —
an error frame alone would also be consistent with a forwarded, then failed, RPC.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_engine_bridge import (  # noqa: E402
    ADMIN_HEADERS,
    DEV_HEADERS,
    _identities,  # noqa: F401 — autouse fixture: seeds the demo + admin accounts
    _rpc,
    client,  # noqa: F401 — fixture
    daemon,  # noqa: F401 — fixture
)

# Params are the real shapes worker_rpc.SUBSCRIPTION_METHODS accepts, so a frame
# that did slip through would be a working call rather than a rejected one.
SUBSCRIPTION_CALLS = (
    ("subscription_login", {"provider": "claude", "device": True}),
    ("subscription_logout", {"provider": "claude"}),
    ("subscription_switch", {"provider": "xai", "account_id": "xai-one"}),
    ("subscription_status", {}),
    ("subscription_accounts", {"provider": "xai"}),
    ("subscription_usage", {"provider": "xai", "force": True}),
)


@pytest.mark.parametrize(("method", "params"), SUBSCRIPTION_CALLS)
def test_standard_user_cannot_reach_operator_subscription_rpc(
    daemon, client, method, params
):
    with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
        _rpc(ws, 1, "hello")
        resp = _rpc(ws, 2, method, params)

    assert resp == {
        "id": 2,
        "error": {
            "code": -32003,
            "message": "administrator authorization required",
        },
    }
    # The point of the gate: the operator's credential store is never touched,
    # which is only true if the frame never left the gateway.
    assert [call["method"] for call in daemon.calls] == ["hello"]


@pytest.mark.parametrize(("method", "params"), SUBSCRIPTION_CALLS)
def test_admin_can_reach_operator_subscription_rpc(daemon, client, method, params):
    with client.websocket_connect("/engine", headers=ADMIN_HEADERS) as ws:
        _rpc(ws, 1, "hello")
        resp = _rpc(ws, 2, method, params)

    assert resp["result"]["ok"] is True
    forwarded = [call for call in daemon.calls if call["method"] == method]
    assert len(forwarded) == 1
    assert forwarded[0]["params"] == params


def test_refusal_keeps_the_bridge_open(daemon, client):
    """A refused frame must ``continue`` the read loop, not drop the session.

    Closing on refusal would take the caller's whole engine connection — every
    open document with it — down over a Settings panel poking a gated RPC.
    """
    with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
        _rpc(ws, 1, "hello")
        assert _rpc(ws, 2, "subscription_status")["error"]["code"] == -32003
        assert _rpc(ws, 3, "subscription_login", {"provider": "xai"})["error"][
            "code"
        ] == -32003
        # Same socket, after two refusals: still serving ordinary traffic.
        assert _rpc(ws, 4, "ping")["result"]["pong"] is True
        assert _rpc(ws, 5, "doc_list")["result"]["docs"] == []


def test_unlisted_subscription_name_is_not_gated_by_the_prefix(daemon, client):
    """Documents why the gate is an explicit set and not ``startswith``.

    ``_OPERATOR_SUBSCRIPTION_METHODS`` names its six members, so a
    ``subscription_``-prefixed RPC nobody has classified is still forwarded — as
    asserted here. That is the intended failure mode: adding a subscription RPC
    breaks this assertion and forces a policy decision at review, instead of the
    new method quietly inheriting whichever verdict the prefix implied.
    """
    from gateway import engine_bridge

    assert "subscription_teapot" not in engine_bridge._OPERATOR_SUBSCRIPTION_METHODS
    with client.websocket_connect("/engine", headers=DEV_HEADERS) as ws:
        _rpc(ws, 1, "hello")
        resp = _rpc(ws, 2, "subscription_teapot", {"provider": "xai"})

    assert resp["result"]["ok"] is True
    assert any(call["method"] == "subscription_teapot" for call in daemon.calls)

