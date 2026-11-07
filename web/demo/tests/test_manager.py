"""Queue, admission, expiry, cleanup and adoption (fake clock, fake podman)."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from demo.manager import ACTIVE, QUEUED, DemoManager, JoinRefused
from demo.podman import NAME_PREFIX, ContainerInfo, PodmanError

from .support import FakeBroker, FakeClock, FakeRuntime, ip_for, make_cfg, sid_for

GIB = 1024 ** 3
PLENTY = (64 * GIB, 500 * GIB)


def run(coro):
    return asyncio.run(coro)


def build(tmp_path, *, capacity=PLENTY, **overrides):
    clock = FakeClock()
    cfg = make_cfg(tmp_path, **overrides)
    runtime, broker = FakeRuntime(), FakeBroker()
    state = {"capacity": capacity}
    mgr = DemoManager(cfg, runtime, broker, clock=clock, capacity_probe=lambda: state["capacity"],
                      port_free=lambda port: True)
    cfg.session_root.mkdir(parents=True, exist_ok=True)
    return mgr, runtime, broker, clock, state


async def settle(mgr):
    await mgr.tick()
    await mgr.wait_idle()


def test_fifo_admission_fills_three_slots_then_queues_in_order(tmp_path):
    async def main():
        mgr, runtime, broker, clock, _ = build(tmp_path)
        for n in range(1, 6):
            mgr.join(sid_for(n), ip_for(n))
        await settle(mgr)
        states = [mgr.entries[sid_for(n)].state for n in range(1, 6)]
        assert states == [ACTIVE] * 3 + [QUEUED] * 2
        assert [s.sid for s in runtime.started] == [sid_for(1), sid_for(2), sid_for(3)]
        assert mgr.status(sid_for(4))["position"] == 1
        assert mgr.status(sid_for(5))["position"] == 2
        assert mgr.status(sid_for(4))["queue_length"] == 2

        await mgr.end(sid_for(2))
        await settle(mgr)
        # Strictly first come, first served: 4 is admitted, 5 moves up.
        assert mgr.entries[sid_for(4)].state == ACTIVE
        assert mgr.entries[sid_for(5)].state == QUEUED and mgr.status(sid_for(5))["position"] == 1
        assert [s.sid for s in runtime.started][-1] == sid_for(4)
        assert sum(1 for e in mgr.entries.values() if e.state == ACTIVE) == 3
    run(main())


def test_admitted_session_gets_its_own_loopback_port_and_directory(tmp_path):
    async def main():
        mgr, runtime, broker, clock, _ = build(tmp_path)
        for n in (1, 2):
            mgr.join(sid_for(n), ip_for(n))
        await settle(mgr)
        ports = {s.port for s in runtime.started}
        assert len(ports) == 2 and all(18300 <= p < 18364 for p in ports)
        for spec in runtime.started:
            assert spec.run_dir == mgr.cfg.session_root / spec.sid / "run"
            assert spec.sid in broker.opened
    run(main())


def test_queued_entry_must_keep_polling_or_it_is_dropped(tmp_path):
    async def main():
        mgr, runtime, broker, clock, _ = build(tmp_path, max_active=1, port_count=4)
        mgr.join(sid_for(1), ip_for(1))
        mgr.join(sid_for(2), ip_for(2))
        mgr.join(sid_for(3), ip_for(3))
        await settle(mgr)
        clock.advance(59)
        mgr.status(sid_for(2))            # polled: keeps its place
        await mgr.tick()
        assert sid_for(2) in mgr.entries and sid_for(3) in mgr.entries
        clock.advance(2)                  # 61 s since 3's last poll, 2 s since 2's
        await mgr.tick()
        assert sid_for(3) not in mgr.entries and sid_for(2) in mgr.entries
        dropped = mgr.status(sid_for(3))
        assert dropped["state"] == "expired" and dropped["note"] == "queue_timeout"
        assert mgr.status(sid_for(2))["position"] == 1
        # A dropped entry never gets a container.
        await mgr.end(sid_for(1))
        await settle(mgr)
        assert [s.sid for s in runtime.started] == [sid_for(1), sid_for(2)]
    run(main())


def test_idle_expiry_destroys_container_directory_and_socket(tmp_path):
    async def main():
        mgr, runtime, broker, clock, _ = build(tmp_path)
        hooks = []

        async def hook(sid):
            hooks.append(sid)
        mgr.add_end_hook(hook)
        sid = sid_for(1)
        mgr.join(sid, ip_for(1))
        await settle(mgr)
        session_dir = mgr.session_dir(sid)
        assert session_dir.is_dir()
        clock.advance(899)
        await mgr.tick()
        assert mgr.entries[sid].state == ACTIVE
        mgr.touch(sid)                    # activity resets the idle clock
        clock.advance(899)
        await mgr.tick()
        assert mgr.entries[sid].state == ACTIVE
        clock.advance(2)
        await mgr.tick()
        assert sid not in mgr.entries
        assert runtime.removed == [NAME_PREFIX + sid] and not runtime.containers
        assert not session_dir.exists()
        assert broker.closed == [sid] and hooks == [sid]
        assert mgr.status(sid)["state"] == "expired" and mgr.status(sid)["note"] == "idle"
    run(main())


def test_hard_expiry_applies_even_to_a_busy_session(tmp_path):
    async def main():
        mgr, runtime, broker, clock, _ = build(tmp_path)
        sid = sid_for(1)
        mgr.join(sid, ip_for(1))
        await settle(mgr)
        for _ in range(44):               # busy: never idle for 15 minutes
            clock.advance(60)
            mgr.touch(sid)
            await mgr.tick()
        assert mgr.entries[sid].state == ACTIVE      # 44 min
        clock.advance(60)
        mgr.touch(sid)
        await mgr.tick()
        assert sid not in mgr.entries                # 45 min
        assert mgr.status(sid)["note"] == "hard_limit"
        assert runtime.removed == [NAME_PREFIX + sid]
    run(main())


def test_warn_flag_before_hard_expiry_and_before_idle_expiry(tmp_path):
    async def main():
        mgr, _, _, clock, _ = build(tmp_path)
        sid = sid_for(1)
        mgr.join(sid, ip_for(1))
        await settle(mgr)
        started = clock()
        status = mgr.status(sid)
        assert status["state"] == "active" and status["warn"] is False
        assert status["hard_expires_at"] == started + 2700
        assert status["idle_expires_at"] == started + 900

        # Idle approach: 599 s quiet = 301 s of idle time left, 600 s = exactly 300.
        clock.advance(599)
        assert mgr.status(sid)["warn"] is False
        clock.advance(1)
        assert mgr.status(sid)["warn"] is True
        mgr.touch(sid)
        assert mgr.status(sid)["warn"] is False

        # Hard approach (kept busy, so idle never triggers): 301 s left, then 300 s left.
        clock.now = started + 2399
        mgr.touch(sid)
        assert mgr.status(sid)["warn"] is False
        clock.now = started + 2400
        mgr.touch(sid)
        assert mgr.status(sid)["warn"] is True       # 300 s left
    run(main())


def test_status_polling_does_not_count_as_activity(tmp_path):
    async def main():
        mgr, _, _, clock, _ = build(tmp_path)
        sid = sid_for(1)
        mgr.join(sid, ip_for(1))
        await settle(mgr)
        for _ in range(16):
            clock.advance(60)
            mgr.status(sid)               # the app shell polls every 15 s
            await mgr.tick()
        assert sid not in mgr.entries and mgr.status(sid)["note"] == "idle"
    run(main())


def test_start_failure_cleans_up_and_admits_the_next_visitor(tmp_path):
    async def main():
        mgr, runtime, broker, clock, _ = build(tmp_path, max_active=1, port_count=4)
        runtime.ready_results = [False, True]
        mgr.join(sid_for(1), ip_for(1))
        mgr.join(sid_for(2), ip_for(2))
        await settle(mgr)
        failed = mgr.status(sid_for(1))
        assert failed["state"] == "expired" and failed["note"] == "start_failed"
        assert runtime.removed == [NAME_PREFIX + sid_for(1)]
        assert not mgr.session_dir(sid_for(1)).exists() and sid_for(1) in broker.closed
        await settle(mgr)
        assert mgr.entries[sid_for(2)].state == ACTIVE
    run(main())


def test_podman_run_error_is_a_clean_failure(tmp_path):
    async def main():
        mgr, runtime, _, _, _ = build(tmp_path)
        runtime.start_error = PodmanError("podman run failed (rc=125)")
        mgr.join(sid_for(1), ip_for(1))
        await settle(mgr)
        assert sid_for(1) not in mgr.entries
        assert mgr.status(sid_for(1))["note"] == "start_failed"
        assert not mgr.session_dir(sid_for(1)).exists()
    run(main())


@pytest.mark.parametrize("probe,expected_note", [
    ((1 * GIB, 500 * GIB), "capacity"),         # MemAvailable below 4 GiB
    ((64 * GIB, 2 * GIB), "capacity"),          # disk below 3 GiB
    ((None, 500 * GIB), "capacity"),            # unknown reading fails closed
])
def test_low_host_capacity_holds_the_queue_with_a_note(tmp_path, probe, expected_note):
    async def main():
        mgr, runtime, _, _, state = build(tmp_path, capacity=probe)
        mgr.join(sid_for(1), ip_for(1))
        await settle(mgr)
        status = mgr.status(sid_for(1))
        assert status["state"] == "queued" and status["note"] == expected_note
        assert status["eta_seconds"] is None and runtime.started == []
        state["capacity"] = PLENTY
        await settle(mgr)
        assert mgr.entries[sid_for(1)].state == ACTIVE and mgr.status(sid_for(1))["note"] is None
    run(main())


def test_sessions_still_starting_count_against_the_memory_floor(tmp_path):
    async def main():
        # 7 GiB free, 4 GiB floor, 1 GiB reserved per starting session: a session is
        # admitted while 7 - starting >= 4, i.e. with 0, 1, 2 or 3 already starting.
        mgr, runtime, _, _, _ = build(tmp_path, capacity=(7 * GIB, 500 * GIB), max_active=6, port_count=8)
        runtime.hang_ready = {sid_for(n) for n in range(1, 7)}
        for n in range(1, 7):
            mgr.join(sid_for(n), ip_for(n))
        await mgr.tick()
        assert sum(1 for e in mgr.entries.values() if e.state == "starting") == 4
        for e in mgr.entries.values():
            if e.start_task:
                e.start_task.cancel()
        await asyncio.gather(*[e.start_task for e in mgr.entries.values() if e.start_task],
                             return_exceptions=True)
    run(main())


def test_one_entry_per_client_address(tmp_path):
    async def main():
        mgr, _, _, clock, _ = build(tmp_path)
        mgr.join(sid_for(1), ip_for(1))
        with pytest.raises(JoinRefused) as refused:
            mgr.join(sid_for(2), ip_for(1))               # same network, other cookie
        assert (refused.value.code, refused.value.status) == ("ip_limit", 409)
        assert mgr.join(sid_for(1), ip_for(1)) is mgr.entries[sid_for(1)]   # same cookie: idempotent
        mgr.join(sid_for(3), ip_for(3))                   # different network is fine
        await mgr.end(sid_for(1))
        mgr.join(sid_for(2), ip_for(1))                   # slot for that network is free again
    run(main())


def test_join_rate_limit_and_queue_length_cap(tmp_path):
    async def main():
        mgr, _, _, clock, _ = build(tmp_path, join_limit=2, join_window=600, max_queue=2, max_active=1,
                                    port_count=4)
        for n in (1, 2):
            mgr.join(sid_for(n), ip_for(9))
            await mgr.end(sid_for(n))
        with pytest.raises(JoinRefused) as limited:
            mgr.join(sid_for(3), ip_for(9))
        assert limited.value.code == "rate_limited" and limited.value.status == 429
        assert 1 <= limited.value.retry_after <= 601
        clock.advance(601)
        mgr.join(sid_for(3), ip_for(9))

        await settle(mgr)                                 # 3 becomes the one active session
        mgr.join(sid_for(4), ip_for(4))
        mgr.join(sid_for(5), ip_for(5))
        with pytest.raises(JoinRefused) as full:
            mgr.join(sid_for(6), ip_for(6))
        assert (full.value.code, full.value.status) == ("queue_full", 503)
    run(main())


def test_eta_comes_from_remaining_session_time(tmp_path):
    async def main():
        mgr, _, _, clock, _ = build(tmp_path, max_active=1, port_count=4)
        for n in (1, 2, 3):
            mgr.join(sid_for(n), ip_for(n))
        await settle(mgr)
        # Active session frees its slot at min(hard, idle) = 900 s; each queued visitor
        # ahead is assumed to hold a slot for half the hard lifetime until history exists.
        assert mgr.status(sid_for(2))["eta_seconds"] == 900
        assert mgr.status(sid_for(3))["eta_seconds"] == 900 + 1350
        clock.advance(300)
        assert mgr.status(sid_for(2))["eta_seconds"] == 600
        mgr.touch(sid_for(1))
        assert mgr.status(sid_for(2))["eta_seconds"] == 900
    run(main())


def test_eta_assumes_the_median_of_recent_session_durations_for_those_ahead(tmp_path):
    async def main():
        mgr, _, _, clock, _ = build(tmp_path, max_active=1, port_count=4)
        for n in (1, 2, 3):
            mgr.join(sid_for(n), ip_for(n))
            await settle(mgr)
            clock.advance(120)
            await mgr.end(sid_for(n))                     # three 120 s sessions of history
        mgr.join(sid_for(4), ip_for(4))
        mgr.join(sid_for(5), ip_for(5))
        await settle(mgr)
        assert mgr.status(sid_for(5))["eta_seconds"] == 900   # sid 4 active, idle expiry 900 s
        assert mgr._typical_duration() == 120
        mgr.join(sid_for(6), ip_for(6))
        assert mgr.status(sid_for(6))["eta_seconds"] == 900 + 120
    run(main())


def test_end_destroys_everything_and_is_idempotent(tmp_path):
    async def main():
        mgr, runtime, broker, _, _ = build(tmp_path)
        sid = sid_for(1)
        mgr.join(sid, ip_for(1))
        await settle(mgr)
        assert await mgr.end(sid) is True
        assert await mgr.end(sid) is False
        assert runtime.removed == [NAME_PREFIX + sid]
        assert not mgr.session_dir(sid).exists() and broker.closed == [sid]
        assert mgr.status(sid)["state"] == "expired" and mgr.status(sid)["note"] == "ended"
        assert mgr.admitted(sid) is None
    run(main())


def test_leaving_the_queue_frees_the_place_and_client_key(tmp_path):
    async def main():
        mgr, runtime, _, _, _ = build(tmp_path, max_active=1, port_count=4)
        mgr.join(sid_for(1), ip_for(1))
        mgr.join(sid_for(2), ip_for(2))
        await settle(mgr)
        await mgr.end(sid_for(2))
        assert mgr.queue == [] and ip_for(2) not in mgr.ip_index
        assert runtime.removed == [] and sid_for(2) not in mgr.entries   # never had a container
    run(main())


def test_ending_a_session_while_it_is_still_starting(tmp_path):
    async def main():
        mgr, runtime, broker, _, _ = build(tmp_path)
        runtime.hang_ready = {sid_for(1)}
        mgr.join(sid_for(1), ip_for(1))
        await mgr.tick()
        await asyncio.sleep(0)
        assert mgr.entries[sid_for(1)].state == "starting"
        assert mgr.admitted(sid_for(1)) is None               # not proxied until healthy
        await mgr.end(sid_for(1))
        assert sid_for(1) not in mgr.entries
        assert runtime.removed == [NAME_PREFIX + sid_for(1)]
        assert not mgr.session_dir(sid_for(1)).exists()
    run(main())


def test_proxied_traffic_touch_only_applies_to_active_sessions(tmp_path):
    async def main():
        mgr, _, _, clock, _ = build(tmp_path, max_active=1, port_count=4)
        mgr.join(sid_for(1), ip_for(1))
        mgr.join(sid_for(2), ip_for(2))
        await settle(mgr)
        before = mgr.entries[sid_for(2)].last_activity
        clock.advance(10)
        mgr.touch(sid_for(2))
        assert mgr.entries[sid_for(2)].last_activity == before
        mgr.touch(sid_for(1))
        assert mgr.entries[sid_for(1)].last_activity == clock()
    run(main())


def test_status_for_unknown_and_missing_cookie(tmp_path):
    async def main():
        mgr, _, _, _, _ = build(tmp_path)
        none = mgr.status(None)
        assert none["state"] == "none" and none["position"] is None and none["warn"] is False
        assert none["ai_remaining_today"] == 45 and none["max_sessions"] == 3
        # A genuine cookie whose session is gone (restart without adoption, long expiry).
        assert mgr.status(sid_for(7))["state"] == "expired"
        for key in ("state", "position", "queue_length", "eta_seconds", "idle_expires_at",
                    "hard_expires_at", "warn"):
            assert key in none
    run(main())


def test_wait_for_change_returns_as_soon_as_the_session_is_admitted(tmp_path):
    async def main():
        mgr, _, _, _, _ = build(tmp_path, max_active=1, port_count=4)
        mgr.join(sid_for(1), ip_for(1))
        mgr.join(sid_for(2), ip_for(2))
        await settle(mgr)
        waiter = asyncio.create_task(mgr.wait_for_change(sid_for(2), 20))
        await asyncio.sleep(0.05)
        assert not waiter.done()
        begun = time.monotonic()
        await mgr.end(sid_for(1))
        await settle(mgr)
        await asyncio.wait_for(waiter, 3)
        assert time.monotonic() - begun < 3
        assert mgr.entries[sid_for(2)].state == ACTIVE
    run(main())


def test_a_held_status_request_keeps_the_queue_place(tmp_path):
    async def main():
        mgr, _, _, clock, _ = build(tmp_path, max_active=1, port_count=4)
        mgr.join(sid_for(1), ip_for(1))
        mgr.join(sid_for(2), ip_for(2))
        await settle(mgr)
        waiter = asyncio.create_task(mgr.wait_for_change(sid_for(2), 0.3))
        await asyncio.sleep(0.1)
        clock.advance(100)                 # a throttled tab's next poll would be this late
        await waiter
        await mgr.tick()
        assert sid_for(2) in mgr.entries   # the held request refreshed last_poll
    run(main())


def test_reconcile_ends_sessions_whose_container_vanished_and_removes_strays(tmp_path):
    async def main():
        mgr, runtime, _, _, _ = build(tmp_path)
        for n in (1, 2):
            mgr.join(sid_for(n), ip_for(n))
        await settle(mgr)
        runtime.containers.pop(NAME_PREFIX + sid_for(1))           # OOM-killed
        runtime.containers[NAME_PREFIX + sid_for(9)] = ContainerInfo(
            NAME_PREFIX + sid_for(9), "running", sid_for(9), 18399, 1, "x")
        await mgr.reconcile()
        assert sid_for(1) not in mgr.entries and mgr.status(sid_for(1))["note"] == "container_exited"
        assert mgr.entries[sid_for(2)].state == ACTIVE
        assert NAME_PREFIX + sid_for(9) in runtime.removed
        assert NAME_PREFIX + sid_for(2) not in runtime.removed
    run(main())


def test_graceful_shutdown_ends_every_session(tmp_path):
    async def main():
        mgr, runtime, broker, _, _ = build(tmp_path, max_active=2, port_count=4)
        runtime.hang_ready = {sid_for(2)}
        for n in (1, 2, 3):
            mgr.join(sid_for(n), ip_for(n))
        await settle_no_wait(mgr)
        await mgr.shutdown()
        assert not mgr.entries and not runtime.containers
        assert not any(mgr.cfg.session_root.iterdir())
        assert set(broker.closed) >= {sid_for(1), sid_for(2)}

    async def settle_no_wait(mgr):
        await mgr.tick()
        await asyncio.sleep(0.05)
    run(main())


# -- adoption ----------------------------------------------------------------------------------
def labelled(clock, n, *, started_ago=100, state="running", port=18300, ip=None, sid=None):
    sid = sid or sid_for(n)
    return ContainerInfo(NAME_PREFIX + sid, state, sid, port + n, int(clock() - started_ago),
                         ip if ip is not None else ip_for(n))


def prepare_dir(mgr, sid, usage=None):
    run_dir = mgr.session_dir(sid) / "run"
    run_dir.mkdir(parents=True)
    if usage is not None:
        (mgr.session_dir(sid) / "usage.json").write_text(json.dumps(usage))


def test_startup_adopts_labelled_containers_and_removes_everything_else(tmp_path):
    async def main():
        mgr, runtime, broker, clock, _ = build(tmp_path)
        good = labelled(clock, 1, started_ago=600)
        prepare_dir(mgr, good.sid, {"requests": 3, "tokens": 5000})
        expired = labelled(clock, 2, started_ago=2701)
        prepare_dir(mgr, expired.sid)
        no_dir = labelled(clock, 3)
        exited = labelled(clock, 4, state="exited")
        prepare_dir(mgr, exited.sid)
        dupe_ip = labelled(clock, 5, ip=good.ip_tag)
        prepare_dir(mgr, dupe_ip.sid)
        bad_label = ContainerInfo(NAME_PREFIX + "nothex", "running", "nothex", 18310, 1, "x")
        unlabelled = ContainerInfo(NAME_PREFIX + sid_for(6), "running", "", None, None, "")
        for info in (good, expired, no_dir, exited, dupe_ip, bad_label, unlabelled):
            runtime.containers[info.name] = info
        (mgr.cfg.session_root / ("f" * 24)).mkdir()            # stale directory, no container

        await mgr.startup(run_loop=False)

        assert list(mgr.entries) == [good.sid]
        entry = mgr.entries[good.sid]
        assert entry.state == ACTIVE and entry.port == good.port
        assert entry.activated_at == good.started
        assert (entry.usage.requests, entry.usage.tokens) == (3, 5000)
        status = mgr.status(good.sid)
        assert status["hard_expires_at"] == good.started + 2700
        assert good.sid in broker.opened                            # provider socket re-created
        assert mgr.ip_index[good.ip_tag] == good.sid
        assert set(runtime.containers) == {good.name}
        assert [p.name for p in mgr.cfg.session_root.iterdir()] == [good.sid]
        # An adopted session expires like any other.
        clock.advance(2700 - 600)
        await mgr.tick()
        assert good.sid not in mgr.entries and not runtime.containers
    run(main())


def test_adoption_respects_the_active_slot_cap_and_health(tmp_path):
    async def main():
        mgr, runtime, _, clock, _ = build(tmp_path, max_active=2, port_count=4)
        infos = [labelled(clock, n) for n in (1, 2, 3)]
        for info in infos:
            prepare_dir(mgr, info.sid)
            runtime.containers[info.name] = info
        await mgr.adopt()
        assert len(mgr.entries) == 2 and len(runtime.containers) == 2

        mgr2, runtime2, _, clock2, _ = build(tmp_path / "b")
        sick = labelled(clock2, 1)
        prepare_dir(mgr2, sick.sid)
        runtime2.containers[sick.name] = sick
        runtime2.healthy_result = False
        await mgr2.adopt()
        assert not mgr2.entries and not runtime2.containers
    run(main())


def test_nothing_is_adopted_when_the_cookie_key_is_ephemeral(tmp_path):
    async def main():
        mgr, runtime, broker, clock, _ = build(tmp_path, cookie_key_persistent=False)
        info = labelled(clock, 1)
        prepare_dir(mgr, info.sid)
        runtime.containers[info.name] = info
        await mgr.startup(run_loop=False)
        assert not mgr.entries and not runtime.containers and broker.opened == {}
        assert not any(mgr.cfg.session_root.iterdir())
    run(main())
