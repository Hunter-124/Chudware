"""Admin (Cloudflare Access JWT), metrics honesty, persistence and quotas."""

from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from demo.manager import ACTIVE
from demo.podman import build_run_args

from .support import front_door, ip_for, make_cfg, sid_for

TEAM = "https://team.example.com"
AUD = "test-aud-token"
EMAIL = "haedmonds69@gmail.com"
ORIGIN_HEADERS = {"host": "app.chudware.com", "origin": "https://app.chudware.com"}
# Requests through Cloudflare carry cf-ray; without any CF headers a loopback
# peer is the operator's SSH break-glass (tested separately).
PUBLIC = {**ORIGIN_HEADERS, "cf-ray": "8f00-LHR"}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


def make_jwt(key, *, email=EMAIL, aud=AUD, iss=TEAM, expires_in=600, kid="test1") -> str:
    now = int(time.time())
    head = _b64(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
    body = _b64(json.dumps({"iss": iss, "aud": aud, "email": email,
                            "iat": now, "exp": now + expires_in}).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64(sig)}"


def arm_access(fd, keypair) -> None:
    """Give the app's verifier the test team's public key (no network fetch)."""
    access = fd.app.state.demo.door.access
    access._keys = {"test1": keypair[1]}
    access._keys_at = time.time()


def access_cfg(**over):
    return {"access_team_domain": TEAM, "access_aud": AUD, **over}


def test_admin_is_disabled_without_access_config(tmp_path):
    with front_door(tmp_path) as fd:
        assert fd.client.get("/admin").status_code == 404
        assert fd.client.get("/admin/api/metrics").status_code == 401
        assert fd.client.post("/admin/login", json={"key": "x" * 40}).status_code in (404, 405)


def test_admin_requires_a_valid_access_jwt_for_the_operator_email(tmp_path, keypair):
    with front_door(tmp_path, **access_cfg()) as fd:
        arm_access(fd, keypair)
        assert fd.client.get("/admin").status_code == 200
        # Public path (cf-ray present) without a JWT: refused.
        assert fd.client.get("/admin/api/metrics", headers=PUBLIC).status_code == 401
        key, _ = keypair
        good = {"cf-access-jwt-assertion": make_jwt(key)}
        body = fd.client.get("/admin/api/metrics", headers={**PUBLIC, **good})
        assert body.status_code == 200
        data = body.json()
        assert data["admin_email"] == EMAIL and data["limits"]["max_sessions"] == 3
        assert data["host"]["disk_free_bytes"] > 0 and "site" in data
        # Every other claim shape is refused.
        for token in (make_jwt(key, email="someone-else@example.com"),
                      make_jwt(key, aud="other-app"),
                      make_jwt(key, iss="https://evil.example.com"),
                      make_jwt(key, expires_in=-10),
                      make_jwt(rsa.generate_private_key(public_exponent=65537, key_size=2048)),
                      "not-a-jwt"):
            resp = fd.client.get("/admin/api/metrics",
                                 headers={**PUBLIC, "cf-access-jwt-assertion": token})
            assert resp.status_code == 401, token[:40]
        # The page itself stays no-store.
        assert fd.client.get("/admin").headers["cache-control"] == "no-store"


def test_loopback_without_cloudflare_headers_is_the_break_glass(tmp_path):
    with front_door(tmp_path, **access_cfg()) as fd:
        # TestClient peer is 127.0.0.1 and sends no CF headers: the SSH-tunnel path.
        assert fd.client.get("/admin/api/metrics").status_code == 200


def test_pageview_counting_excludes_assets_polls_and_404s(tmp_path):
    with front_door(tmp_path, **access_cfg()) as fd:
        fd.client.get("/")                       # landing document: a pageview
        fd.client.get("/demo/status")            # poll: not a pageview
        fd.client.get("/demo/health")            # probe: not a pageview
        fd.client.get("/no-such-page")           # scan/404: not a pageview
        fd.client.get("/demo/static/waiting.js")  # asset: not a pageview
        metrics = fd.app.state.demo.metrics.snapshot()
        assert metrics["pageviews_total"] == 1
        assert metrics["routes"]["status_poll"] == 1
        assert metrics["statuses"]["404"] >= 1
        assert metrics["today"]["pageviews"] == 1 and metrics["requests_total"] >= 5


def test_workspace_survives_sleep_and_resumes_with_the_same_cookie(tmp_path):
    with front_door(tmp_path) as fd:
        visitor = fd.visitor()
        visitor.join()
        visitor.wait_for("active")
        sid = next(iter(fd.manager.entries))
        saved = fd.manager.workspace_dir(sid) / "work" / "bracket.step+"
        saved.write_text("feature history")
        assert visitor.end().json()["state"] == "sleeping"
        assert saved.read_text() == "feature history"
        # Same cookie (the visitor object kept it): resumes, second container, same files.
        assert visitor.join().status_code == 200
        visitor.wait_for("active")
        assert saved.read_text() == "feature history"
        assert len(fd.runtime.started) == 2
        assert fd.runtime.started[1].workspace_dir == fd.manager.workspace_dir(sid)
        # Explicit delete is the only way the files go away.
        visitor.end()
        deleted = visitor.post("/demo/workspace/delete", headers={"origin": "https://app.chudware.com"})
        assert deleted.status_code == 200 and not fd.manager.workspace_dir(sid).exists()


def test_persistent_mounts_replace_work_and_data_tmpfs(tmp_path):
    cfg = make_cfg(tmp_path)
    from demo.podman import SessionSpec
    spec = SessionSpec(sid=sid_for(1), port=18300, run_dir=tmp_path / "run", started=1,
                       ip_tag=ip_for(1), workspace_dir=tmp_path / "ws" / sid_for(1))
    args = build_run_args(cfg, spec)
    joined = "\0".join(args)
    assert f"{tmp_path}/ws/{sid_for(1)}/work:/work:rw,nosuid,nodev,U" in joined
    assert f"{tmp_path}/ws/{sid_for(1)}/data:/data:rw,nosuid,nodev,U" in joined
    tmpfs_args = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--tmpfs"]
    assert not any(v.startswith("/work:") or v.startswith("/data:") for v in tmpfs_args)
    assert any(v.startswith("/state:") for v in tmpfs_args)   # state stays ephemeral


def test_workspace_quota_sleeps_but_never_deletes(tmp_path):
    async def main():
        from demo.manager import DemoManager
        from .support import FakeBroker, FakeClock, FakeRuntime, PLENTY
        clock = FakeClock()
        cfg = make_cfg(tmp_path, workspace_quota_bytes=10, quota_check_seconds=1)
        mgr = DemoManager(cfg, FakeRuntime(), FakeBroker(), clock=clock,
                          capacity_probe=lambda: PLENTY, port_free=lambda p: True)
        cfg.session_root.mkdir(parents=True, exist_ok=True)
        sid = sid_for(1)
        mgr.join(sid, ip_for(1))
        await mgr.tick(); await mgr.wait_idle()
        assert mgr.entries[sid].state == ACTIVE
        (mgr.workspace_dir(sid) / "work" / "big.bin").write_bytes(b"x" * 100)
        clock.advance(2)
        await mgr.tick()
        assert sid not in mgr.entries
        status = mgr.status(sid)
        assert status["state"] == "sleeping" and status["note"] == "quota_exceeded"
        assert (mgr.workspace_dir(sid) / "work" / "big.bin").exists()   # kept, not deleted
    asyncio.run(main())


def test_admin_can_sleep_a_session_and_delete_a_workspace(tmp_path, keypair):
    with front_door(tmp_path, **access_cfg()) as fd:
        arm_access(fd, keypair)
        visitor = fd.visitor()
        visitor.join()
        visitor.wait_for("active")
        sid = next(iter(fd.manager.entries))
        headers = {**PUBLIC, "cf-access-jwt-assertion": make_jwt(keypair[0])}
        ended = fd.client.post("/admin/api/session/end", headers=headers, json={"sid": sid[:8]})
        assert ended.status_code == 200
        assert visitor.get("/demo/status").json()["state"] == "sleeping"
        deleted = fd.client.post("/admin/api/workspace/delete", headers=headers, json={"sid": sid})
        assert deleted.status_code == 200 and not fd.manager.workspace_dir(sid).exists()
        # Without the JWT (public path) both actions refuse.
        assert fd.client.post("/admin/api/session/end", headers=PUBLIC, json={"sid": sid}).status_code == 401
        assert fd.client.post("/admin/api/workspace/delete", headers=PUBLIC, json={"sid": sid}).status_code == 401


def test_site_metrics_unconfigured_and_query_shape(tmp_path):
    import asyncio as _asyncio

    from demo.site_metrics import SiteMetrics

    async def main():
        assert await SiteMetrics("", "").snapshot() == {"configured": False}
    _asyncio.run(main())
