"""Actor WorkerBridge contract, with compiled fake workers and a fixed-path launcher.

No providers or existing user daemons are used. Bubblewrap supplies disposable /opt
and /state; an optional built-hostd integration exercises gateway control/events.
The fake launcher records the launch contract, not production sandbox policy.
The restart case runs unchanged production Python workers and persistent handle
registries inside private actor mounts; it does not start an online agent turn.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux actor cells")

_STUBS = r'''
#include "hostd/hostd_log.h"
#include "security/runtime_guard.h"
#include "util/platform_paths.h"
#include <sys/prctl.h>
#include <signal.h>
#include <unistd.h>
namespace hostd {
Log& log() { static Log instance; return instance; }
void Log::line(const char*, ...) {}
}
namespace chudware::platform {
std::string executablePath() {
    char path[4096]; auto count = readlink("/proc/self/exe", path, sizeof(path));
    return count > 0 ? std::string(path, count) : std::string();
}
void guardParentDeath(long parent) {
    prctl(PR_SET_PDEATHSIG, SIGKILL);
    if (getppid() != parent) _exit(125);
}
void closeInheritedFds(int lowest) { for (int fd = lowest; fd < 4096; ++fd) close(fd); }
}
namespace chudware::security {
IntegrityResult verifyInstalledPayload(const std::filesystem::path&, VerifyMode) {
    IntegrityResult result; result.ok = true; return result;
}
RuntimeGuardProbes platformRuntimeGuardProbes() { return {}; }
RuntimeGuardDecision inspectAndPersistRuntime(SecurityCheckpoint,
    const std::filesystem::path&, const std::vector<FileVerdict>&,
    std::string_view, RuntimeGuardProbes, std::int64_t) { return {}; }
std::filesystem::path runtimeGuardStateDir() { return "/state"; }
}
'''

_HARNESS = r'''
#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <utility>
#include <nlohmann/json.hpp>
// Test-local access to age the real pool clock, not a production test hook.
#define private public
#include "hostd/worker_bridge.h"
#undef private
#include "hostd/worker_bridge.cpp"
#include <iostream>
#include <thread>
#include <vector>
#include <mutex>
#include <signal.h>
using nlohmann::json;
std::mutex outputMutex;
void emit(const json& value) {
    std::lock_guard<std::mutex> lock(outputMutex);
    std::cout << value.dump() << std::endl;
}
int main() {
    signal(SIGPIPE, SIG_IGN);
    std::vector<std::thread> calls;
    std::string line;
    while (std::getline(std::cin, line)) {
        json request = json::parse(line);
        if (request["op"] == "shutdown") break;
        calls.emplace_back([request] {
            json result; std::string err; bool ok = false, matched = false;
            const auto ctx = request.value("ctx", json::object());
            if (request["op"] == "dispatch") {
                ok = hostd::WorkerBridge::dispatch(request["method"],
                    request.value("params", json::object()), result, err,
                    request.value("timeout_ms", 5000), [request](const json& event) {
                        emit({{"tag", request["tag"]}, {"event", event}});
                    }, ctx);
            } else if (request["op"] == "control") {
                ok = hostd::WorkerBridge::controlActiveRun(request["action"],
                    request["params"], matched, result, err, ctx);
            } else if (request["op"] == "revoke") {
                hostd::WorkerBridge::revokeActor(request["actor_key"],
                    request.at("workspace_key"),
                    request.at("revoked_through").get<int64_t>());
                ok = true;
            } else if (request["op"] == "activity") {
                const auto activity = hostd::WorkerBridge::activity();
                result = {{"busy", activity.busy}, {"background", activity.background},
                          {"unknown", activity.unknown}};
                ok = true;
            } else if (request["op"] == "reap") {
                auto lanes = hostd::WorkerBridge::actorLanes(ctx, false, err);
                if (lanes) {
                    {
                        std::lock_guard<std::mutex> lock(lanes->state.mutex);
                        for (auto* extra : lanes->state.extras)
                            lanes->state.slots.at(extra).lastEnded =
                                std::chrono::steady_clock::now() - std::chrono::minutes(11);
                    }
                    hostd::WorkerBridge::reapIdleExtras(lanes->state);
                }
                ok = true;
            }
            emit({{"tag", request["tag"]}, {"ok", ok}, {"matched", matched},
                  {"result", result}, {"err", err}});
        });
    }
    hostd::WorkerBridge::shutdownIfCreated();
    for (auto& thread : calls) thread.join();
}
'''

_ACTIVATE = r'''
#include <cstdlib>
#include <fcntl.h>
#include <unistd.h>
extern char** environ;
int main(int argc, char** argv) {
    if (argc < 3) return 94;
    if (dup2(std::atoi(argv[1]), 3) < 0 || fcntl(3, F_SETFD, 0) < 0) return 95;
    execve(argv[2], argv + 2, environ);
    return 96;
}
'''

_LAUNCHER = r'''
#include <nlohmann/json.hpp>
#include <filesystem>
#include <fstream>
#include <unistd.h>
extern char** environ;
int main(int argc, char** argv) {
    if (argc != 7 || std::string(argv[2]) != "rw" || std::string(argv[3]) != "--" ||
        std::string(argv[4]) != "/opt/chudware/bin/chudware-worker" ||
        std::string(argv[5]) != "--module" || std::string(argv[6]) != "worker") return 91;
    const auto root = std::filesystem::path("/state/actors") / argv[1];
    std::filesystem::create_directories(root / "run");
    nlohmann::json record = {{"argv", nlohmann::json::array()}, {"env", nlohmann::json::object()}};
    for (int i = 0; i < argc; ++i) record["argv"].push_back(argv[i]);
    for (char** row = environ; *row; ++row) {
        const std::string value(*row); const auto split = value.find('=');
        record["env"][value.substr(0, split)] = value.substr(split + 1);
    }
    std::ofstream(root / ("launch-" + std::to_string(getpid()) + ".json")) << record;
    execve(argv[4], argv + 4, environ);
    return 92;
}
'''

_WORKER = r'''
#include <nlohmann/json.hpp>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <sys/socket.h>
#include <sys/un.h>
#include <signal.h>
#include <unistd.h>
using nlohmann::json;
int main() {
    const std::string actor = std::getenv("CHUDWARE_ACTOR_KEY");
    const std::string hex = std::getenv("CHUDWARE_ACTOR_HEX");
    const std::string root = "/state/actors/" + hex;
    const std::string suffix = "/run/control-" + std::to_string(getpid()) + ".sock";
    const std::string endpoint = root + suffix;
    const int listener = socket(AF_UNIX, SOCK_STREAM, 0);
    sockaddr_un address{}; address.sun_family = AF_UNIX;
    endpoint.copy(address.sun_path, sizeof(address.sun_path) - 1);
    if (bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) ||
        listen(listener, 8)) return 93;
    std::mutex mutex; std::condition_variable changed;
    bool cancelled = false; std::string active;
    bool ignoreCancel = false;
    std::atomic<unsigned> background{0};
    std::set<std::string> sessions;
    json infoCtx;
    std::thread([&] {
        for (;;) {
            const int fd = accept(listener, nullptr, nullptr);
            if (fd < 0) return;
            std::string line; char ch;
            while (read(fd, &ch, 1) == 1 && ch != '\n') line += ch;
            const auto req = json::parse(line);
            json result;
            {
                std::lock_guard<std::mutex> lock(mutex);
                const auto ctx = req.value("ctx", json::object());
                const bool owner = ctx.value("actor_key", std::string()) == actor;
                const auto action = req.value("action", std::string());
                const auto params = req.value("params", json::object());
                const bool matched = owner && params.value("run_id", std::string()) == active;
                if (action == "cancel" && matched && !ignoreCancel) {
                    cancelled = true; changed.notify_all();
                }
                if (action == "release" && owner) {
                    sessions.erase(params.value("session_id", std::string()));
                    std::ofstream(root + "/release-" + std::to_string(getpid()) + ".json") << req;
                }
                result = {{"ok", owner}, {"active", matched}, {"cancelled", matched},
                          {"ctx", ctx}, {"actor", actor}, {"pid", getpid()}};
            }
            const auto response = json{{"ok", true}, {"result", result}}.dump() + "\n";
            write(fd, response.data(), response.size()); close(fd);
        }
    }).detach();
    std::string line;
    while (std::getline(std::cin, line)) {
        const auto req = json::parse(line);
        const std::string method = req.value("method", std::string());
        if (method == "shutdown") break;
        const auto ctx = req.value("ctx", json::object());
        const auto params = req.value("params", json::object());
        json result;
        if (method == "worker_control_info") {
            infoCtx = ctx;
            std::string reported = "/actor" + suffix;
            std::ifstream bad(root + "/bad-endpoint");
            if (bad) std::getline(bad, reported);
            result = {{"transport", "unix"}, {"endpoint", reported}, {"token", std::string(64, 'a')}};
        } else if (method == "agent_handles_new") {
            auto mint = [](const char* kind) {
                unsigned char bytes[16];
                std::ifstream random("/dev/urandom", std::ios::binary);
                random.read(reinterpret_cast<char*>(bytes), sizeof(bytes));
                if (!random) _exit(97);
                constexpr const char* hex = "0123456789abcdef";
                std::string value = std::string(kind) + "_";
                for (const auto byte : bytes) {
                    value += hex[byte >> 4]; value += hex[byte & 15];
                }
                return value;
            };
            result = {{"ok", true}, {"session_id", mint("session")}, {"run_id", mint("run")}};
        } else if (method == "agent_admin_turn") {
            std::unique_lock<std::mutex> lock(mutex);
            active = params.at("run_id"); cancelled = false;
            ignoreCancel = params.value("ignore_cancel", false);
            if (ignoreCancel) signal(SIGTERM, SIG_IGN);
            sessions.insert(params.at("session_id"));
            std::cout << json{{"id", req["id"]}, {"event", "started"},
                {"data", {{"kind", "run_started"}, {"run_id", active}, {"pid", getpid()}, {"ctx", ctx},
                          {"info_ctx", infoCtx}}}}.dump() << std::endl;
            if (params.value("finish_file", false)) {
                lock.unlock();
                while (!std::filesystem::exists(root + "/finish-turn-" + std::to_string(getpid()) +
                                               "-" + params.at("run_id").get<std::string>()))
                    std::this_thread::sleep_for(std::chrono::milliseconds(10));
                lock.lock();
            } else changed.wait(lock, [&] { return cancelled; });
            result = {{"pid", getpid()}, {"ctx", ctx}, {"cancelled", cancelled}, {"completed", !cancelled}};
            active.clear();
        } else if (method == "chudcode_test_background" || method == "git_gh_auth_login") {
            background.fetch_add(1);
            const auto finish = root + "/finish-background-" + std::to_string(getpid());
            std::thread([&, finish] {
                while (!std::filesystem::exists(finish))
                    std::this_thread::sleep_for(std::chrono::milliseconds(10));
                std::ofstream(root + "/background-finished-" + std::to_string(getpid())) << "completed";
                background.fetch_sub(1);
            }).detach();
            result = {{"pid", getpid()}};
        } else if (method == "worker_activity") {
            std::ofstream(root + "/activity-ctx-" + std::to_string(getpid()) + ".json") << ctx;
            int delay = 0;
            std::ifstream(root + "/activity-delay") >> delay;
            if (delay > 0) std::this_thread::sleep_for(std::chrono::milliseconds(delay));
            result = {{"ok", true}, {"active", background.load()},
                      {"by_kind", {{"fixture_background", background.load()}}}, {"complete", true}};
            if (std::filesystem::exists(root + "/activity-malformed")) result["active"] = "invalid";
            if (std::filesystem::exists(root + "/activity-oversized"))
                result["by_kind"][std::string(100000, 'a')] = 0;
            if (std::filesystem::exists(root + "/activity-inconsistent"))
                result["by_kind"]["inconsistent_counter"] = 1;
        } else {
            std::lock_guard<std::mutex> lock(mutex);
            result = {{"pid", getpid()}, {"actor", actor}, {"ctx", ctx}, {"sessions", sessions},
                      {"info_ctx", infoCtx}, {"lane", std::getenv("CHUDWARE_HOSTD_WORKER_LANE")}};
        }
        std::cout << json{{"id", req["id"]}, {"ok", true}, {"result", result}}.dump() << std::endl;
    }
    unlink(endpoint.c_str());
    _exit(0);
}
'''


class Harness:
    def __init__(self, command, state):
        self.state = state
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True,
                                        env={**os.environ, "CHUDWARE_AGENT_WORKERS": "2",
                                             "AMBIENT_SECRET": "do-not-inherit",
                                             "OPENAI_API_KEY": "daemon-key-must-not-leak",
                                             "CHUDWARE_WORKER_BIN": "/untrusted/worker",
                                             "CHUDWARE_PYTHON": "/untrusted/python"})
        self.condition = threading.Condition()
        self.messages = []
        self.counter = 0
        self.writer = threading.Lock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            with self.condition:
                self.messages.append(json.loads(line))
                self.condition.notify_all()

    def send(self, **request):
        with self.writer:
            self.counter += 1
            tag = self.counter
            self.process.stdin.write(json.dumps({"tag": tag, **request}) + "\n")
            self.process.stdin.flush()
        return tag

    def receive(self, tag, event=False, timeout=20):
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                for index, message in enumerate(self.messages):
                    if message["tag"] == tag and ("event" in message) == event:
                        return self.messages.pop(index)
                remaining = deadline - time.monotonic()
                assert remaining > 0, f"missing response for {tag}: {self.messages}"
                self.condition.wait(remaining)

    def call(self, **request):
        return self.receive(self.send(**request))

    def turn(self, ctx, run, session, **params):
        tag = self.send(op="dispatch", method="agent_admin_turn", ctx=ctx, timeout_ms=-1,
                        params={"run_id": run, "session_id": session, **params})
        return tag, self.receive(tag, event=True)["event"]

    def control(self, ctx, run, action="cancel"):
        return self.call(op="control", action=action, ctx=ctx, params={"run_id": run})

    def close(self):
        if self.process.poll() is None:
            self.send(op="shutdown")
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
                raise
        self.reader.join(timeout=2)
        assert self.process.returncode == 0


@pytest.fixture(scope="module")
def cell_payload(tmp_path_factory):
    compiler = shutil.which("g++")
    bwrap = shutil.which("bwrap")
    if not compiler or not bwrap:
        pytest.skip("C++ compiler and bubblewrap required for isolated fixed-path fixture")
    includes = [Path("/usr/include")]
    includes.extend(p.parents[1] for p in ROOT.glob("**/single_include/nlohmann/json.hpp"))
    include = next((p for p in includes if (p / "nlohmann/json.hpp").is_file()), None)
    if include is None:
        pytest.skip("nlohmann C++ headers unavailable")
    probe = subprocess.run([bwrap, "--unshare-user", "--ro-bind", "/", "/", "--",
                            "/usr/bin/true"], capture_output=True, text=True)
    if probe.returncode:
        pytest.skip("unprivileged bubblewrap unavailable: " + probe.stderr.strip())
    directory = tmp_path_factory.mktemp("cell-worker-payload")
    bindir = directory / "bin"
    bindir.mkdir()
    flags = [compiler, "-std=c++17", "-pthread", "-DCHUDWARE_DEVELOPER_BUILD",
             "-I", str(ROOT / "core"), "-I", str(include)]
    sources = {}
    for name, content in {"stubs": _STUBS, "harness": _HARNESS,
                          "launcher": _LAUNCHER, "worker": _WORKER, "activate": _ACTIVATE}.items():
        source = directory / (name + ".cpp")
        source.write_text(content)
        sources[name] = source
    subprocess.run([*flags, str(sources["harness"]), str(sources["stubs"]),
                    "-o", str(bindir / "harness")], check=True)
    for name, filename in (("launcher", "chudware-actor-sandbox"), ("worker", "chudware-worker"),
                           ("activate", "activate")):
        subprocess.run([*flags, str(sources[name]), "-o", str(bindir / filename)], check=True)
    return bwrap, bindir


@pytest.fixture
def cell(cell_payload, tmp_path, request):
    bwrap, bindir = cell_payload
    missing = getattr(request, "param", None)
    if missing:
        bindir = shutil.copytree(bindir, tmp_path / "bin")
        (bindir / missing).unlink()
    state = tmp_path / "state"
    state.mkdir()
    command = [bwrap, "--unshare-user", "--unshare-pid", "--die-with-parent",
               "--tmpfs", "/", "--ro-bind", "/usr", "/usr"]
    for directory in ("/lib", "/lib64"):
        if Path(directory).exists():
            command += ["--ro-bind", directory, directory]
    command += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--ro-bind", str(bindir), "/opt/chudware/bin", "--bind", str(state), "/state",
                "--", "/opt/chudware/bin/harness"]
    harness = Harness(command, state)
    try:
        yield harness
    finally:
        harness.close()


def context(actor, epoch=1):
    return {"workspace_key": "team:org/team", "actor_key": actor,
            "actor_hex": hashlib.sha256(actor.encode()).hexdigest()[:32],
            "right": "editor", "epoch": epoch, "client_id": 17, "cell": True}


def inspect(cell, ctx, session=None):
    params = {} if session is None else {"session_id": session}
    response = cell.call(op="dispatch", method="chudcode_test_inspect", ctx=ctx, params=params)
    assert response["ok"], response
    return response["result"]


def test_actor_process_registry_context_and_minimal_environment(cell):
    alice, bob = context("alice"), context("bob")
    a_tag, a_started = cell.turn(alice, "a-run", "same-session")
    b_tag, b_started = cell.turn(bob, "b-run", "same-session")
    assert a_started["pid"] != b_started["pid"]
    for ctx, event in ((alice, a_started), (bob, b_started)):
        assert event["ctx"] == ctx
        assert event["info_ctx"] == ctx  # internal endpoint acquisition is scoped too
        records = list((cell.state / "actors" / ctx["actor_hex"]).glob("launch-*.json"))
        assert len(records) == 1
        record = json.loads(records[0].read_text())
        assert record["argv"][1:] == [ctx["actor_hex"], "rw", "--",
                                          "/opt/chudware/bin/chudware-worker", "--module", "worker"]
        env = record["env"]
        assert "AMBIENT_SECRET" not in env
        assert "OPENAI_API_KEY" not in env
        assert set(env) == {
            "PATH", "LANG", "LC_ALL", "HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
            "XDG_RUNTIME_DIR", "XDG_CACHE_HOME", "TMPDIR", "CHUDWARE_CELL",
            "CHUDWARE_CHUDCODE_BIN", "CHUDWARE_HOSTD_SOCKET", "CHUDWARE_NATIVE_ROOT",
            "CHUDWARE_FS_ROOT", "CHUDWARE_PROJECT_DIR", "CHUDWARE_PROJECT_SAVE_DIR",
            "CHUDWARE_WORKSPACE_KEY", "CHUDWARE_ACTOR_KEY", "CHUDWARE_ACTOR_HEX",
            "CHUDWARE_HOSTD_WORKER_LANE"}
        assert "CHUDWARE_WORKER_BIN" not in env and "CHUDWARE_PYTHON" not in env
        assert env["CHUDWARE_HOSTD_SOCKET"] == "/actor/run/hostd.sock"
        assert env["HOME"] == "/actor/home" and "CHUDWARE_CELL_STATE" not in env
        assert env["CHUDWARE_NATIVE_ROOT"] == "/work"
        assert env["CHUDWARE_ACTOR_KEY"] == ctx["actor_key"]
    cross = cell.control(bob, "a-run", action="status")
    assert not cross["ok"] and not cross["matched"]
    for ctx, run, tag in ((alice, "a-run", a_tag), (bob, "b-run", b_tag)):
        control = cell.control(ctx, run)
        assert control["ok"] and control["matched"]
        assert control["result"]["ctx"] == ctx
        assert cell.receive(tag)["ok"]
    assert inspect(cell, alice, "same-session")["pid"] == a_started["pid"]
    assert inspect(cell, bob, "same-session")["pid"] == b_started["pid"]
    a_general = cell.call(op="dispatch", method="test_inspect", ctx=alice)["result"]
    b_general = cell.call(op="dispatch", method="test_inspect", ctx=bob)["result"]
    assert len({a_general["pid"], b_general["pid"], a_started["pid"], b_started["pid"]}) == 4
    assert a_general["ctx"] == alice and b_general["ctx"] == bob


def test_concurrent_turns_keep_actor_pool_and_session_owners(cell):
    alice, bob = context("alice"), context("bob")
    first, first_event = cell.turn(alice, "a-one", "one")
    second, second_event = cell.turn(alice, "a-two", "two")
    other, other_event = cell.turn(bob, "b-one", "one")
    assert len({first_event["pid"], second_event["pid"], other_event["pid"]}) == 3
    for ctx, run, tag in ((alice, "a-one", first), (alice, "a-two", second), (bob, "b-one", other)):
        assert cell.control(ctx, run)["ok"]
        assert cell.receive(tag)["ok"]
    assert inspect(cell, alice, "one")["pid"] == first_event["pid"]
    assert inspect(cell, alice, "two")["pid"] == second_event["pid"]
    assert inspect(cell, bob, "one")["sessions"] == ["one"]


def test_busy_session_owner_releases_before_transfer_in_actor_pool(cell):
    alice = context("alice")
    first, first_event = cell.turn(alice, "one", "session-one")
    assert cell.control(alice, "one")["ok"]
    assert cell.receive(first)["ok"]
    busy, busy_event = cell.turn(alice, "two", "session-two")
    assert busy_event["pid"] == first_event["pid"]
    moved, moved_event = cell.turn(alice, "three", "session-one")
    assert moved_event["pid"] != first_event["pid"]
    released = cell.state / "actors" / alice["actor_hex"] / f"release-{first_event['pid']}.json"
    request = json.loads(released.read_text())
    assert request["ctx"] == alice
    assert request["params"] == {"session_id": "session-one"}
    for run, tag in (("two", busy), ("three", moved)):
        assert cell.control(alice, run)["ok"]
        assert cell.receive(tag)["ok"]
    assert inspect(cell, alice, "session-two")["sessions"] == ["session-two"]
    assert inspect(cell, alice, "session-one")["sessions"] == ["session-one"]


def test_revoke_wakes_pool_waiters_without_respawn_and_fresh_epoch_works(cell):
    alice, bob = context("alice"), context("bob")
    one, one_event = cell.turn(alice, "a-one", "one")
    two, _ = cell.turn(alice, "a-two", "two")
    waiting = cell.send(op="dispatch", method="agent_admin_turn", timeout_ms=-1, ctx=alice,
                        params={"run_id": "a-waiting", "session_id": "one"})
    other, other_event = cell.turn(bob, "b-run", "one")
    revoked = cell.call(op="revoke", actor_key=alice["actor_key"],
                        workspace_key=alice["workspace_key"], revoked_through=alice["epoch"])
    assert revoked["ok"]
    assert not cell.receive(waiting)["ok"]
    cell.receive(one)
    cell.receive(two)
    assert not cell.control(alice, "a-one", action="status")["matched"]
    stale = cell.call(op="dispatch", method="chudcode_test_inspect", ctx=alice)
    assert stale["result"]["code"] == "revoked"
    assert len(list((cell.state / "actors" / alice["actor_hex"]).glob("launch-*.json"))) == 2
    alive = cell.control(bob, "b-run", action="status")
    assert alive["ok"] and alive["result"]["pid"] == other_event["pid"]
    fresh = context("alice", epoch=2)
    new = inspect(cell, fresh)
    assert new["pid"] != one_event["pid"] and new["sessions"] == []
    assert new["ctx"] == fresh
    assert cell.control(bob, "b-run")["ok"]
    assert cell.receive(other)["ok"]


def test_revoke_bounds_uncooperative_actor_without_harming_other_actor(cell):
    alice, bob = context("alice"), context("bob")
    wedged, _ = cell.turn(alice, "wedged", "one", ignore_cancel=True)
    other, _ = cell.turn(bob, "other", "one")
    assert cell.call(op="revoke", actor_key=alice["actor_key"],
                     workspace_key=alice["workspace_key"], revoked_through=alice["epoch"])["ok"]
    result = cell.receive(wedged)
    assert not result["ok"]
    assert result["result"]["code"] == "worker_lost_mid_turn"
    assert cell.control(bob, "other", action="status")["ok"]
    assert cell.control(bob, "other")["ok"]
    assert cell.receive(other)["ok"]
    assert inspect(cell, context("alice", epoch=2))["sessions"] == []


@pytest.mark.parametrize("endpoint", ["/state/actors/other/run/ctrl.sock",
                                     "/actor/../other/run/ctrl.sock", "/tmp/ctrl.sock"])
def test_foreign_or_traversing_control_endpoint_is_refused(cell, endpoint):
    alice = context("alice")
    actor = cell.state / "actors" / alice["actor_hex"]
    actor.mkdir(parents=True)
    (actor / "bad-endpoint").write_text(endpoint)
    response = cell.call(op="dispatch", method="agent_admin_turn", ctx=alice,
                         params={"run_id": "bad", "session_id": "session"})
    assert not response["ok"]
    assert "outside owning actor" in response["err"]


@pytest.mark.parametrize("cell", ["chudware-actor-sandbox", "chudware-worker"], indirect=True)
def test_missing_actor_launcher_or_compiled_worker_fails_closed(cell):
    response = cell.call(op="dispatch", method="test_inspect", ctx=context("alice"))
    assert not response["ok"]
    assert ("actor sandbox launcher unavailable" in response["err"] or
            "Compiled worker not found" in response["err"])
    assert not list(cell.state.glob("actors/*/launch-*.json"))


class GatewayConnection:
    """Raw adopted-listener client; never discovers or spawns a desktop daemon."""

    def __init__(self, path, actor=None, workspace=None, epoch=0, right="editor", operator=False):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(20)
        self.socket.connect(str(path))
        self.stream = self.socket.makefile("rb")
        self.next_id = 0
        self.events = []
        params = {
            "client": "gui", "proto": 1,
            "principal": {"workspace_key": workspace, "actor_key": actor,
                          "right": right, "epoch": epoch,
                          "connection_nonce": uuid.uuid4().hex}}
        if operator:
            params = {"client": "control", "operator": True, "proto": 1}
        deadline = time.monotonic() + 60
        while True:
            self.hello = self.call("hello", params)
            if "error" in self.hello:
                break  # stale-principal callers inspect the canonical rejection
            if operator:
                break  # operator hello cannot be replayed; census observes load state
            state = self.hello["result"]["state"]
            assert state != "failed", self.hello
            if state == "ready":
                break
            assert time.monotonic() < deadline, "scratch cell engine did not load"
            time.sleep(0.05)

    def send(self, method, params=None):
        self.next_id += 1
        self.socket.sendall((json.dumps({"id": self.next_id, "method": method,
                                        "params": params or {}}) + "\n").encode())
        return self.next_id

    def receive(self):
        line = self.stream.readline()
        assert line, "cell gateway disconnected before expected reply/event"
        return json.loads(line)

    def reply(self, request_id):
        while True:
            message = self.receive()
            if "event" in message:
                self.events.append(message)
            elif message.get("id") == request_id:
                return message

    def call(self, method, params=None):
        return self.reply(self.send(method, params))

    def run_event(self, kind, run_id):
        while True:
            for index, message in enumerate(self.events):
                data = message.get("data", {})
                if (message.get("event") == "agent.event" and
                        data.get("kind") == kind and data.get("run_id") == run_id):
                    return self.events.pop(index)["data"]
            message = self.receive()
            assert "event" in message, message
            self.events.append(message)

    def close(self):
        self.stream.close()
        self.socket.close()


@pytest.fixture
def actual_cell_daemon(cell_payload, tmp_path, request):
    from services.mcp import core_bridge, daemon_bridge

    hostd = daemon_bridge.resolve_hostd_bin()
    library = next((p for p in core_bridge._lib_candidates() if p.is_file()), None)
    if hostd is None or not Path(hostd).is_file() or library is None:
        pytest.skip("built hostd and native engine required for cell worker integration")
    bwrap, fake_bin = cell_payload
    payload = tmp_path / "payload"
    bindir = shutil.copytree(fake_bin, payload / "bin")
    shutil.copy2(hostd, bindir / "chudware-hostd")
    if getattr(request, "param", False):
        # Exercise unchanged production workers/registries for restart continuity,
        # not the compiled control fixture. No online turn or provider is invoked.
        python = ROOT / ".venv-mcp/bin/python"
        if not python.is_file():
            pytest.skip("development worker venv required for real session resumption")
        bootstrap = ("import runpy,sys; sys.path.insert(0," + repr(str(ROOT)) + "); "
                     "runpy.run_module('services.compiled_entry', run_name='__main__', alter_sys=True)")
        worker = bindir / "chudware-worker"
        worker.write_text(
            "#!/usr/bin/python3\nimport os,sys\nfrom pathlib import Path\n"
            "root=Path('/state/actors')/os.environ['CHUDWARE_ACTOR_HEX']\n"
            "for name in ('home','data','config','cache','run'):\n"
            "    (root/name).mkdir(mode=0o700, exist_ok=True)\n"
            f"os.execv({bwrap!r}, [{bwrap!r}, '--ro-bind', '/', '/', "
            "'--bind', str(root), '/actor', '--tmpfs', '/state', '--tmpfs', '/tmp', "
            "'--setenv', 'CHUDWARE_ACTOR_SANDBOX', '1', '--', "
            f"{str(python)!r}, '-I', '-c', {bootstrap!r}, *sys.argv[1:]])\n",
            encoding="utf-8")
        worker.chmod(0o755)
    contracts = payload / "contracts"
    contracts.mkdir()
    shutil.copy2(ROOT / "contracts/cell_methods.json", contracts / "cell_methods.json")
    state, work = tmp_path / "state", tmp_path / "work"
    state.mkdir(mode=0o700)
    work.mkdir()
    (state / "home").mkdir()
    (state / "data").mkdir()
    # Build-tree libraries may have absolute RPATHs. Only the read-only repository
    # and system runtime are exposed to this disposable test namespace, not HOME.
    # Like CellRuntime, activate clears FD_CLOEXEC on listener fd 3 before exec;
    # bubblewrap passes it through without a descriptor-preservation option.
    command = [bwrap, "--unshare-user", "--unshare-pid", "--die-with-parent",
               "--tmpfs", "/", "--ro-bind", "/usr", "/usr"]
    for directory in ("/lib", "/lib64", "/etc"):
        if Path(directory).exists():
            command += ["--ro-bind", directory, directory]
    command += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--ro-bind", str(ROOT), str(ROOT),
                "--ro-bind", str(payload), "/opt/chudware",
                "--bind", str(state), "/state", "--bind", str(work), "/work"]
    if getattr(request, "param", False):
        command += ["--dir", "/actor"]
        runtime = python.resolve().parent.parent
        if not runtime.is_relative_to(ROOT) and not runtime.is_relative_to("/usr"):
            # uv-managed venv interpreters can live outside the read-only repo.
            command += ["--ro-bind", str(runtime), str(runtime)]
    # Respect explicit engine overrides outside the repository without exposing
    # their parent tree: the library itself gets a private read-only mount.
    library = library.resolve()
    if not library.is_relative_to(ROOT):
        command += ["--ro-bind", str(library), "/engine/libchudware.so"]
        engine_path = "/engine/libchudware.so"
    else:
        engine_path = str(library)
    command += ["--", "/opt/chudware/bin/chudware-hostd", "--cell",
                "--gateway-listen-fd", "3", "--cell-state", "/state", "--cell-work", "/work"]
    env = {"PATH": "/usr/bin:/bin", "HOME": "/state/home", "XDG_DATA_HOME": "/state/data",
           "LANG": "C.UTF-8", "CHUDWARE_LIBCHUDWARE": engine_path,
           "LD_LIBRARY_PATH": str(library.parent),
           "CHUDWARE_AGENT_WORKERS": "2", "CHUDWARE_HOSTD_LINGER_SECS": "60",
           "CHUDWARE_HOSTD_DRAIN_GRACE_S": "3"}
    clients = []
    with tempfile.TemporaryDirectory(prefix="cw-cell-live-") as gateway_dir:
        gateway_path = Path(gateway_dir) / "gw.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(gateway_path))
        listener.listen(8)
        log_path = tmp_path / "cell-console.log"
        with log_path.open("wb") as console:
            def start():
                return subprocess.Popen(
                    [str(bindir / "activate"), str(listener.fileno()), *command],
                    pass_fds=(listener.fileno(),), env=env, stdout=console, stderr=console)

            process = start()

            def stop():
                for client in clients:
                    client.close()
                clients.clear()
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                        pytest.fail("spawned cell did not terminate: " + log_path.read_text())
                assert process.returncode in (0, -signal.SIGTERM), log_path.read_text()

            def connect(actor, workspace, epoch=0, right="editor"):
                client = GatewayConnection(gateway_path, actor, workspace, epoch, right)
                clients.append(client)
                return client

            def operator():
                deadline = time.monotonic() + 60
                path = state / "run/operator.sock"
                while not path.is_socket():
                    assert process.poll() is None
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
                client = GatewayConnection(path, operator=True)
                clients.append(client)
                return client

            def restart():
                nonlocal process
                stop()
                process = start()
                cell["process"] = process

            cell = {"connect": connect, "operator": operator, "state": state,
                    "process": process, "restart": restart}
            try:
                yield cell
            finally:
                stop()
                listener.close()


@pytest.mark.parametrize("actual_cell_daemon", [True], indirect=True)
def test_real_worker_session_resumes_after_hostd_restart(actual_cell_daemon):
    cell = actual_cell_daemon
    alice, bob = str(uuid.uuid4()), str(uuid.uuid4())
    workspace = "team:restart-continuity"
    opened = cell["connect"](alice, workspace).call("agent_handles_new")
    original = opened.get("result")
    assert original and original.get("ok") is True, opened
    cell["restart"]()
    b = cell["connect"](bob, workspace)
    # The restarted hostd has no remembered owner. Resolve only against Bob's
    # genuine persistent registry; a failed guess must not claim Alice's handle.
    unknown = b.call("agent_handles_new", {"session_id": original["session_id"]})
    assert unknown.get("result", {}).get("code") == "not_found", unknown
    a = cell["connect"](alice, workspace)
    resumed = a.call("agent_handles_new", {"session_id": original["session_id"]})
    assert resumed["result"]["ok"] is True, resumed
    assert resumed["result"]["session_id"] == original["session_id"]
    assert resumed["result"]["run_id"] != original["run_id"]
    # Successful resumption rehydrates hostd ownership before any further lane call.
    foreign = b.call("agent_handles_new", {"session_id": original["session_id"]})
    assert foreign.get("error", {}).get("code") == "not_found", foreign


def test_actual_cell_revoke_streams_stop_and_preserves_other_actor(actual_cell_daemon):
    cell = actual_cell_daemon
    alice, bob = str(uuid.uuid4()), str(uuid.uuid4())
    workspace = "team:worker-integration/team"
    a_turn = cell["connect"](alice, workspace)
    a_monitor = cell["connect"](alice, workspace)
    b_turn = cell["connect"](bob, workspace)
    b_control = cell["connect"](bob, workspace)
    for client in (a_turn, a_monitor, b_turn, b_control):
        assert client.hello.get("result", {}).get("cell") is True, client.hello
    a_response = a_turn.call("agent_handles_new")
    b_response = b_turn.call("agent_handles_new")
    a_handles = a_response.get("result")
    b_handles = b_response.get("result")
    assert a_handles and b_handles, (a_response, b_response)
    assert a_handles["run_id"] != b_handles["run_id"]
    a_request = a_turn.send("agent_admin_turn", {**a_handles, "message": "wait"})
    a_started = a_turn.run_event("run_started", a_handles["run_id"])
    b_request = b_turn.send("agent_admin_turn", {**b_handles, "message": "wait"})
    b_started = b_turn.run_event("run_started", b_handles["run_id"])
    assert a_started["pid"] != b_started["pid"]
    for actor, event in ((alice, a_started), (bob, b_started)):
        ctx = event["ctx"]
        assert ctx["actor_key"] == actor
        assert ctx["workspace_key"] == workspace and ctx["epoch"] == 0
        assert ctx["actor_hex"] == hashlib.sha256(actor.encode()).hexdigest()[:32]
        assert set(ctx) == {"cell", "workspace_key", "actor_key", "actor_hex",
                            "right", "epoch", "client_id"}
        assert event["info_ctx"] == ctx
    cross = b_control.call("agent_cancel", {"run_id": a_handles["run_id"]})
    assert cross.get("error", {}).get("code") == "not_found", cross
    for method in ("chudcode_session_stats", "chudcode_close_session",
                   "session_handoff", "agent_handles_new"):
        denied = b_control.call(method, {"session_id": a_handles["session_id"]})
        assert denied.get("error", {}).get("code") == "not_found", (method, denied)
        unknown = b_control.call(method, {"session_id": "absent-session"})
        assert unknown.get("error", {}).get("code") == "worker_lane_busy", (method, unknown)
    own_busy = b_control.call("chudcode_session_stats", {"session_id": b_handles["session_id"]})
    assert own_busy.get("error", {}).get("code") == "worker_lane_busy", own_busy
    revoked = b_control.call("principal_revoke", {"actor_key": alice, "epoch": 1})
    assert revoked.get("result", {}).get("ok") is True, revoked
    stopped = a_monitor.run_event("run_stopped", a_handles["run_id"])
    assert stopped["reason"] == "revoked" and stopped["actor_key"] == alice
    # The explicit event precedes retirement of the actor's gateway connection.
    assert a_monitor.stream.readline() == b""
    assert a_turn.run_event("run_stopped", a_handles["run_id"])["reason"] == "revoked"
    assert a_turn.stream.readline() == b"", f"revoked turn unexpectedly replied: {a_request}"
    a_root = cell["state"] / "actors" / a_started["ctx"]["actor_hex"]
    assert not (a_root / "run" / f"control-{a_started['pid']}.sock").exists()
    assert len(list(a_root.glob("launch-*.json"))) == 1
    # Bob's side channel still reaches the same live process after Alice teardown.
    probe = b_control.call("agent_steer", {"run_id": b_handles["run_id"], "text": "probe"})
    assert probe.get("result", {}).get("active") is True, probe
    assert probe["result"]["pid"] == b_started["pid"]
    assert probe["result"]["ctx"]["actor_key"] == bob
    assert b_control.call("agent_cancel", {"run_id": b_handles["run_id"]})["result"]["cancelled"]
    assert b_turn.reply(b_request)["result"]["cancelled"]
    stats = b_control.call("chudcode_session_stats", {"session_id": b_handles["session_id"]})
    assert stats["result"]["pid"] == b_started["pid"]
    assert stats["result"]["sessions"] == [b_handles["session_id"]]
    assert all(event.get("data", {}).get("run_id") != a_handles["run_id"]
               for event in b_control.events)
    stale = cell["connect"](alice, workspace, epoch=0)
    assert stale.hello.get("error", {}).get("code") == "revoked"
    fresh = cell["connect"](alice, workspace, epoch=2)
    assert fresh.hello.get("result", {}).get("cell") is True
    fresh_handles = fresh.call("agent_handles_new")["result"]
    fresh_stats = fresh.call("chudcode_session_stats", {"session_id": fresh_handles["session_id"]})
    assert fresh_stats["result"]["pid"] != a_started["pid"]
    assert fresh_stats["result"]["sessions"] == []
    manager_actor = str(uuid.uuid4())
    manager = cell["connect"](manager_actor, workspace, right="manager")
    assert manager.hello.get("result", {}).get("cell") is True
    managed_request = fresh.send("agent_admin_turn", {
        "run_id": fresh_handles["run_id"], "session_id": fresh_handles["session_id"],
        "message": "wait for manager stop"})
    managed_started = fresh.run_event("run_started", fresh_handles["run_id"])
    for method, params in (
        ("agent_steer", {"run_id": fresh_handles["run_id"], "text": "not permitted"}),
        ("agent_answer", {"run_id": fresh_handles["run_id"], "answers": {}}),
    ):
        denied = manager.call(method, params)
        assert denied.get("error", {}).get("code") == "not_found", denied
    cancelled = manager.call("agent_cancel", {"run_id": fresh_handles["run_id"]})
    result = cancelled.get("result", {})
    assert result.get("cancelled") is True, cancelled
    assert result["pid"] == managed_started["pid"]
    assert result["ctx"]["actor_key"] == alice  # worker retains the owning actor scope
    assert result["ctx"]["control_delegate"] == {
        "actor_key": manager_actor, "right": "manager", "action": "cancel"}
    assert fresh.reply(managed_request)["result"]["cancelled"]


def test_revoke_without_existing_lanes_blocks_delayed_old_epoch_dispatch(cell):
    actor = context("not-yet-dispatched", epoch=0)
    assert cell.call(op="revoke", actor_key=actor["actor_key"],
                     workspace_key=actor["workspace_key"], revoked_through=1)["ok"]
    for epoch in (0, 1):
        delayed = cell.call(op="dispatch", method="test_inspect",
                            ctx={**actor, "epoch": epoch})
        assert not delayed["ok"]
        assert delayed["result"]["code"] == "revoked"
    assert not (cell.state / "actors" / actor["actor_hex"]).exists()
    fresh = inspect(cell, {**actor, "epoch": 2})
    assert fresh["ctx"]["epoch"] == 2


def test_principal_update_tombstones_prior_epoch_but_admits_new_epoch(cell):
    actor = context("right-update", epoch=0)
    assert cell.call(op="revoke", actor_key=actor["actor_key"],
                     workspace_key=actor["workspace_key"], revoked_through=0)["ok"]
    old = cell.call(op="dispatch", method="test_inspect", ctx=actor)
    assert not old["ok"] and old["result"]["code"] == "revoked"
    assert inspect(cell, {**actor, "epoch": 1})["ctx"]["epoch"] == 1


def test_trusted_manager_cancel_marker_is_forwarded_inside_owner_ctx(cell):
    owner = context("alice")
    tag, event = cell.turn(owner, "managed-run", "owned-session")
    delegated = {**owner, "control_delegate": {
        "actor_key": "manager", "right": "manager", "action": "cancel"}}
    result = cell.control(delegated, "managed-run")
    assert result["ok"] and result["matched"]
    assert result["result"]["ctx"] == delegated
    assert result["result"]["actor"] == owner["actor_key"]
    assert result["result"]["pid"] == event["pid"]
    assert cell.receive(tag)["ok"]


def test_census_never_spawns_and_skips_busy_reserved_and_queued_lanes(cell):
    assert cell.call(op="activity")["result"] == {"busy": 0, "background": 0, "unknown": 0}
    assert not list(cell.state.glob("actors/*/launch-*.json"))
    owner = context("census-busy")
    first, _ = cell.turn(owner, "first", "one")
    second, _ = cell.turn(owner, "second", "two")
    queued = cell.send(op="dispatch", method="agent_admin_turn", ctx=owner, timeout_ms=-1,
                       params={"run_id": "queued", "session_id": "three"})
    start = time.monotonic()
    assert cell.call(op="activity")["result"] == {"busy": 2, "background": 0, "unknown": 0}
    assert time.monotonic() - start < 1
    root = cell.state / "actors" / owner["actor_hex"]
    assert not list(root.glob("activity-ctx-*.json"))
    assert cell.control(owner, "first")["ok"]
    assert cell.receive(first)["ok"]
    cell.receive(queued, event=True)
    for run, tag in (("second", second), ("queued", queued)):
        assert cell.control(owner, run)["ok"]
        assert cell.receive(tag)["ok"]
    assert cell.call(op="activity")["result"] == {"busy": 0, "background": 0, "unknown": 0}
    for record in root.glob("activity-ctx-*.json"):
        assert json.loads(record.read_text())["actor_key"] == owner["actor_key"]


@pytest.mark.parametrize("failure", ["timeout", "malformed", "oversized", "inconsistent"])
def test_bounded_census_failure_keeps_worker_and_next_response_framed(cell, failure):
    owner = context("census-framing")
    before = inspect(cell, owner)
    root = cell.state / "actors" / owner["actor_hex"]
    marker = root / ("activity-delay" if failure == "timeout" else f"activity-{failure}")
    marker.write_text("3500" if failure == "timeout" else "malformed")
    started = time.monotonic()
    census = cell.call(op="activity")["result"]
    assert census["unknown"] == 1 and census["busy"] == 0
    assert time.monotonic() - started < 3
    # Stdio queues behind the old probe, but the delayed probe response is never
    # mistaken for this ordinary call, and its process is never killed/replaced.
    after = inspect(cell, owner)
    assert after["pid"] == before["pid"]
    assert len(list(root.glob("launch-*.json"))) == 1
    marker.unlink()
    assert cell.call(op="activity")["result"]["unknown"] == 0


@pytest.mark.parametrize("unknown", [False, True])
def test_idle_extra_reaper_preserves_real_background_and_unknown_lanes(cell, unknown):
    owner = context("extra-background")
    first, first_event = cell.turn(owner, "one", "one")
    second, second_event = cell.turn(owner, "two", "two")
    for run, tag in (("one", first), ("two", second)):
        assert cell.control(owner, run)["ok"]
        assert cell.receive(tag)["ok"]
    response = cell.call(op="dispatch", method="chudcode_test_background", ctx=owner,
                         params={"session_id": "two"})
    assert response["ok"]
    assert response["result"]["pid"] == second_event["pid"] != first_event["pid"]
    root = cell.state / "actors" / owner["actor_hex"]
    malformed = root / "activity-malformed"
    if unknown:
        malformed.write_text("invalid")
    assert cell.call(op="reap", ctx=owner)["ok"]
    alive = inspect(cell, owner, "two")
    assert alive["pid"] == second_event["pid"]
    if unknown:
        malformed.unlink()
    assert cell.call(op="activity")["result"]["background"] == 1
    (root / f"finish-background-{second_event['pid']}").write_text("finish")
    deadline = time.monotonic() + 5
    while not (root / f"background-finished-{second_event['pid']}").is_file():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    while cell.call(op="activity")["result"]["background"] != 0:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert cell.call(op="reap", ctx=owner)["ok"]
    assert inspect(cell, owner, "two")["pid"] == first_event["pid"]


def test_actual_cell_drain_keeps_turn_reply_and_background_completion(actual_cell_daemon):
    cell = actual_cell_daemon
    actor, workspace = "continuing-actor", "team:drain-continuation"
    customer = cell["connect"](actor, workspace, right="owner")
    handles = [customer.call("agent_handles_new")["result"] for _ in range(3)]
    second_customer = cell["connect"](actor, workspace, right="owner")
    queued_customer = cell["connect"](actor, workspace, right="owner")
    turn = customer.send("agent_admin_turn", {**handles[0], "finish_file": True})
    started = customer.run_event("run_started", handles[0]["run_id"])
    second_turn = second_customer.send("agent_admin_turn", {**handles[1], "finish_file": True})
    second_started = second_customer.run_event("run_started", handles[1]["run_id"])
    queued_turn = queued_customer.send("agent_admin_turn", {**handles[2], "finish_file": True})
    operator = cell["operator"]()
    deadline = time.monotonic() + 5
    while operator.call("cell_census")["result"]["rpc_inflight"] < 3:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    closed = operator.call("cell_drain", {"admission": "closed", "exit": "when_quiescent"})["result"]
    assert closed["sessions"] >= 3 and closed["rpc_inflight"] >= 3 and not closed["quiescent"]
    time.sleep(1)
    assert cell["process"].poll() is None
    actor_hex = hashlib.sha256(actor.encode()).hexdigest()[:32]
    root = cell["state"] / "actors" / actor_hex
    (root / f"finish-turn-{started['pid']}-{handles[0]['run_id']}").write_text("finish")
    reply = customer.reply(turn)
    assert reply["result"]["completed"] and not reply["result"]["cancelled"]
    queued_started = queued_customer.run_event("run_started", handles[2]["run_id"])
    assert cell["process"].poll() is None
    for client, request, event, handle in (
            (second_customer, second_turn, second_started, handles[1]),
            (queued_customer, queued_turn, queued_started, handles[2])):
        (root / f"finish-turn-{event['pid']}-{handle['run_id']}").write_text("finish")
        result = client.reply(request)["result"]
        assert result["completed"] and not result["cancelled"]
        client.close()
    # Existing admitted connection may start follow-on work; drain is not cancel.
    background = customer.call("git_gh_auth_login")["result"]["pid"]
    customer.close()
    census = operator.call("cell_census")["result"]
    assert census["workers"]["background"] == 1 and not census["quiescent"]
    time.sleep(2)
    assert cell["process"].poll() is None
    (root / f"finish-background-{background}").write_text("finish")
    deadline = time.monotonic() + 5
    while not (root / f"background-finished-{background}").is_file():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert cell["process"].wait(timeout=10) == 0


@pytest.mark.parametrize("unknown", [False, True])
def test_actual_cell_background_and_unknown_pin_ten_second_idle(actual_cell_daemon, unknown):
    cell = actual_cell_daemon
    actor, workspace = "idle-background", "team:idle-background"
    customer = cell["connect"](actor, workspace, right="owner")
    response = customer.call("plm_status" if unknown else "git_gh_auth_login")["result"]
    pid = response["pid"]
    root = cell["state"] / "actors" / hashlib.sha256(actor.encode()).hexdigest()[:32]
    malformed = root / "activity-malformed"
    if unknown:
        malformed.write_text("invalid")
    operator = cell["operator"]()
    customer.close()
    time.sleep(11)
    assert cell["process"].poll() is None
    census = operator.call("cell_census")["result"]
    assert not census["quiescent"]
    if unknown:
        assert census["workers"]["unknown"] == 1 and not census["complete"]
        malformed.unlink()
    else:
        assert census["workers"]["background"] == 1 and census["complete"]
        (root / f"finish-background-{pid}").write_text("finish")
        deadline = time.monotonic() + 5
        while not (root / f"background-finished-{pid}").is_file():
            assert time.monotonic() < deadline
            time.sleep(0.02)
    assert cell["process"].wait(timeout=15) == 0


def test_census_has_one_overall_budget_for_many_unresponsive_live_lanes(cell):
    actors = [context("slow-one"), context("slow-two")]
    processes = {}
    for actor in actors:
        processes[(actor["actor_key"], "agent")] = inspect(cell, actor)["pid"]
        general = cell.call(op="dispatch", method="test_inspect", ctx=actor)
        assert general["ok"]
        processes[(actor["actor_key"], "general")] = general["result"]["pid"]
        root = cell.state / "actors" / actor["actor_hex"]
        (root / "activity-delay").write_text("3500")
    started = time.monotonic()
    census = cell.call(op="activity")["result"]
    assert census == {"busy": 0, "background": 0, "unknown": 4}
    assert time.monotonic() - started < 3
    for actor in actors:
        assert inspect(cell, actor)["pid"] == processes[(actor["actor_key"], "agent")]
        general = cell.call(op="dispatch", method="test_inspect", ctx=actor)
        assert general["ok"] and general["result"]["pid"] == processes[(actor["actor_key"], "general")]
        root = cell.state / "actors" / actor["actor_hex"]
        assert len(list(root.glob("launch-*.json"))) == 2
        (root / "activity-delay").unlink()
    assert cell.call(op="activity")["result"]["unknown"] == 0
