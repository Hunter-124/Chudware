"""Admin page, metrics honesty, workspace persistence and quotas."""

from __future__ import annotations

import asyncio

from demo.manager import ACTIVE
from demo.podman import build_run_args

from .support import front_door, ip_for, make_cfg, sid_for

ADMIN_KEY = b"a" * 40
ORIGIN_HEADERS = {"host": "app.chudware.com", "origin": "https://app.chudware.com"}


def _admin_cookie(fd) -> str:
    resp = fd.client.post("/admin/login", headers=ORIGIN_HEADERS, json={"key": ADMIN_KEY.decode()})
    assert resp.status_code == 200
    return resp.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]


def test_admin_is_disabled_without_a_key(tmp_path):
    with front_door(tmp_path) as fd:
        assert fd.client.get("/admin").status_code == 404
        assert fd.client.post("/admin/login", json={"key": "x" * 40}).status_code == 404
        assert fd.client.get("/admin/api/metrics").status_code == 404 or \
            fd.client.get("/admin/api/metrics").status_code == 401


def test_admin_login_and_metrics_flow(tmp_path):
    with front_door(tmp_path, admin_key=ADMIN_KEY) as fd:
        assert fd.client.get("/admin").status_code == 200
        assert fd.client.get("/admin/api/metrics").status_code == 401
        bad = fd.client.post("/admin/login", headers=ORIGIN_HEADERS, json={"key": "wrong"})
        assert bad.status_code == 401
        cookie = _admin_cookie(fd)
        headers = {**ORIGIN_HEADERS, "cookie": f"__Host-cw_admin={cookie}"}
        body = fd.client.get("/admin/api/metrics", headers=headers).json()
        assert body["limits"]["max_sessions"] == 3 and body["host"]["disk_free_bytes"] > 0
        assert "sessions" in body and "workspaces" in body and "daily" in body["traffic"]
        # Visitor cookies never authenticate as admin, and vice versa the page is no-store.
        assert fd.client.get("/admin").headers["cache-control"] == "no-store"


def test_pageview_counting_excludes_assets_polls_and_404s(tmp_path):
    with front_door(tmp_path, admin_key=ADMIN_KEY) as fd:
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
    from .support import FakeRuntime  # noqa: F401  (keeps the support import surface obvious)
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


def test_admin_can_sleep_a_session_and_delete_a_workspace(tmp_path):
    with front_door(tmp_path, admin_key=ADMIN_KEY) as fd:
        visitor = fd.visitor()
        visitor.join()
        visitor.wait_for("active")
        sid = next(iter(fd.manager.entries))
        headers = {**ORIGIN_HEADERS, "cookie": f"__Host-cw_admin={_admin_cookie(fd)}"}
        ended = fd.client.post("/admin/api/session/end", headers=headers, json={"sid": sid[:8]})
        assert ended.status_code == 200
        assert visitor.get("/demo/status").json()["state"] == "sleeping"
        deleted = fd.client.post("/admin/api/workspace/delete", headers=headers, json={"sid": sid})
        assert deleted.status_code == 200 and not fd.manager.workspace_dir(sid).exists()
        # Without the admin cookie both actions refuse.
        assert fd.client.post("/admin/api/session/end", json={"sid": sid}).status_code == 401
        assert fd.client.post("/admin/api/workspace/delete", json={"sid": sid}).status_code == 401
