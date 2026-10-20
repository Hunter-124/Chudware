// Native Windows-only WorkerBridge regression driver. No provider/runtime required.
// Compile this file plus util/platform_paths.cpp (do NOT also compile worker_bridge.cpp).
// The production bridge is included below; security/log substitutions are test-only.
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <iterator>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>
#include <nlohmann/json.hpp>
// Test-local pool clock/ownership access, never a production test hook.
#define private public
#include "hostd/worker_bridge.h"
#undef private
#include "hostd/worker_bridge.cpp"

namespace hostd {
Log& log() { static Log instance; return instance; }
void Log::line(const char*, ...) {}
}
namespace chudware::security {
IntegrityResult verifyInstalledPayload(const std::filesystem::path&, VerifyMode) {
    IntegrityResult result; result.ok = true; return result;
}
RuntimeGuardProbes platformRuntimeGuardProbes() { return {}; }
RuntimeGuardDecision inspectAndPersistRuntime(SecurityCheckpoint,
    const std::filesystem::path&, const std::vector<FileVerdict>&,
    std::string_view, RuntimeGuardProbes, std::int64_t) { return {}; }
std::filesystem::path runtimeGuardStateDir() { return {}; }
}

namespace {
using Bridge = hostd::WorkerBridge;
using Json = nlohmann::json;
using Clock = std::chrono::steady_clock;
const std::string token(64, 'a');
void require(bool ok, const std::string& message) {
    if (!ok) throw std::runtime_error(message);
}
template<class F> void eventually(F predicate, const std::string& message, int ms = 5000) {
    const auto until = Clock::now() + std::chrono::milliseconds(ms);
    while (!predicate()) {
        require(Clock::now() < until, message);
        Sleep(5);
    }
}
std::string env(const char* name) {
    const char* value = std::getenv(name);
    return value ? value : "";
}
void emit(const Json& value) { std::cout << value.dump() << std::endl; }

PROCESS_INFORMATION startSelf(const std::wstring& arguments, const std::filesystem::path& executable = {},
                              bool inheritStdio = false) {
    const auto path = (executable.empty() ? std::filesystem::u8path(chudware::platform::executablePath())
                                         : executable).wstring();
    std::wstring command = L"\"" + path + L"\" " + arguments;
    STARTUPINFOW info{}; info.cb = sizeof(info);
    if (inheritStdio) {
        info.dwFlags = STARTF_USESTDHANDLES;
        info.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
        info.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
        info.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    }
    PROCESS_INFORMATION child{};
    require(CreateProcessW(path.c_str(), command.data(), nullptr, nullptr, inheritStdio,
                          CREATE_NO_WINDOW, nullptr, nullptr, &info, &child) != FALSE,
            "fixture subprocess launch failed: " + std::to_string(GetLastError()));
    CloseHandle(child.hThread);
    child.hThread = nullptr;
    return child;
}
bool alive(DWORD pid) {
    HANDLE process = OpenProcess(SYNCHRONIZE, FALSE, pid);
    if (!process) return false;
    const bool result = WaitForSingleObject(process, 0) == WAIT_TIMEOUT;
    CloseHandle(process);
    return result;
}
DWORD descendant(bool inheritStdio = false) {
    auto child = startSelf(L"--descendant", {}, inheritStdio);
    CloseHandle(child.hProcess);
    return child.dwProcessId;
}

struct Fixture {
    std::mutex mutex;
    std::condition_variable changed;
    std::string active;
    std::set<std::string> sessions;
    bool cancelled = false, ignoreCancel = false, refuseRelease = false;
    int infoCalls = 0;
    Json activity{{"ok", true}, {"complete", true}, {"active", 0}, {"by_kind", Json::object()}};
    int activityDelay = 0;
    bool malformedActivity = false;
    std::string steer;
    Json answers = Json::object();
    std::string endpoint;

    void serve(HANDLE pipe) {
        for (;;) {
            if (!ConnectNamedPipe(pipe, nullptr) && GetLastError() != ERROR_PIPE_CONNECTED) return;
            std::string frame;
            char chunk[4096]; DWORD count = 0;
            while (frame.find('\n') == std::string::npos && frame.size() <= 65536) {
                if (!ReadFile(pipe, chunk, sizeof(chunk), &count, nullptr) || !count) break;
                frame.append(chunk, count);
            }
            auto request = Json::parse(frame, nullptr, false);
            Json reply{{"ok", false}, {"error", {{"message", "fixture refusal"}}}};
            std::string raw;
            if (request.is_object() && request.value("token", "") == token) {
                const auto params = request.value("params", Json::object());
                const auto failure = params.value("_fail", "");
                if (failure == "timeout") Sleep(3000);
                if (failure == "closed" || failure == "timeout") {
                    DisconnectNamedPipe(pipe); continue;
                }
                if (failure == "malformed") raw = "not-json\n";
                else if (failure == "oversized") raw = std::string(256 * 1024 + 1, 'x') + "\n";
                else if (failure != "refused") {
                    std::lock_guard<std::mutex> lock(mutex);
                    const auto action = request.value("action", "");
                    const bool matched = !active.empty() && params.value("run_id", "") == active;
                    Json result = Json::object();
                    if (action == "cancel") {
                        if (matched && !ignoreCancel) { cancelled = true; changed.notify_all(); }
                        result = {{"cancelled", matched}};
                    } else if (action == "status") result = {{"active", matched}};
                    else if (action == "steer") {
                        if (matched) steer = params.value("text", "");
                        result = {{"steered", matched}};
                    } else if (action == "answer") {
                        if (matched) answers = params.value("answers", Json::object());
                        result = {{"ok", matched}, {"accepted", matched}};
                    } else if (action == "release") {
                        const auto sid = params.value("session_id", "");
                        if (refuseRelease) result = {{"ok", false}, {"reason", "session_busy"}};
                        else { sessions.erase(sid); result = {{"ok", true}, {"released", true}}; }
                    }
                    reply = {{"ok", true}, {"result", result}};
                }
            }
            if (raw.empty()) raw = reply.dump() + "\n";
            std::size_t sent = 0;
            while (sent < raw.size()) {
                if (!WriteFile(pipe, raw.data() + sent,
                               static_cast<DWORD>(std::min<std::size_t>(4096, raw.size() - sent)),
                               &count, nullptr) || !count) break;
                sent += count;
            }
            // Wait for the client to consume the frame and close, without a flush
            // that could retain an oversized response after its client refuses it.
            ReadFile(pipe, chunk, 1, &count, nullptr);
            // No peer-controlled FlushFileBuffers wait in this fixture.
            DisconnectNamedPipe(pipe);
        }
    }

    void startControl() {
        const auto user = chudware_ipc::localProcessUser(GetCurrentProcess());
        LPWSTR sid = nullptr;
        require(ConvertSidToStringSidW(chudware_ipc::localUserSid(user), &sid) != FALSE,
                "fixture SID lookup failed");
        const auto owner = hostd::nativeUtf8(std::filesystem::path(sid).native());
        LocalFree(sid);
        char suffix[9]; std::snprintf(suffix, sizeof(suffix), "%08lx", GetCurrentProcessId());
        endpoint = "\\\\.\\pipe\\chudware-control-" + owner + "-" + std::string(56, 'b') + suffix;
        auto& security = chudware_ipc::privateSecurity();
        require(security.valid(), "fixture private security unavailable");
        for (int i = 0; i < 4; ++i) {
            const DWORD access = PIPE_ACCESS_DUPLEX | (i == 0 ? FILE_FLAG_FIRST_PIPE_INSTANCE : 0);
            HANDLE pipe = CreateNamedPipeW(std::filesystem::u8path(endpoint).c_str(), access,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                4, 65536, 65536, 0, &security.attributes);
            require(pipe != INVALID_HANDLE_VALUE, "fixture private pipe creation failed");
            std::thread([this, pipe] { serve(pipe); }).detach();
        }
    }

    int run() {
        startControl();
        std::string line;
        while (std::getline(std::cin, line)) {
            const auto request = Json::parse(line);
            const int id = request.at("id").get<int>();
            const std::string method = request.at("method").get<std::string>();
            const auto params = request.value("params", Json::object());
            if (method == "shutdown") break;
            Json result{{"pid", GetCurrentProcessId()}, {"lane", env("CHUDWARE_HOSTD_WORKER_LANE")},
                        {"engine", env("CHUDWARE_ENGINE_DAEMON")},
                        {"cwd", hostd::nativeUtf8(std::filesystem::current_path().native())}};
            if (method == "worker_control_info") {
                ++infoCalls;
                result = {{"transport", "named_pipe"}, {"endpoint", endpoint}, {"token", token}};
            } else if (method == "worker_activity") {
                if (activityDelay) Sleep(activityDelay);
                if (malformedActivity) { emit({{"id", id}, {"ok", true}, {"result", "malformed"}}); continue; }
                result = activity;
            } else if (method == "plm_fixture_large") {
                result["payload"] = params.at("payload");
            } else if (method == "plm_fixture_split") {
                result["payload"] = params.at("payload");
                std::string packet = Json{{"id", id}, {"ok", true}, {"result", result}}.dump();
                if (!params.value("partial_eof", false)) packet += '\n';
                const std::size_t parts = params.value("fragments", 2);
                std::size_t sent = 0;
                for (std::size_t i = 0; i < parts; ++i) {
                    const std::size_t end = packet.size() * (i + 1) / parts;
                    std::cout.write(packet.data() + sent, static_cast<std::streamsize>(end - sent));
                    std::cout.flush();
                    sent = end;
                    if (i + 1 < parts) Sleep(params.value("delay_ms", 0));
                }
                if (params.value("exit", false)) std::_Exit(0);
                continue;
            } else if (method == "session_fixture_release") {
                std::lock_guard<std::mutex> lock(mutex);
                refuseRelease = params.value("refuse", false);
            } else if (method == "plm_fixture_activity" || method == "session_fixture_activity") {
                activity = params.at("value");
                activityDelay = params.value("delay_ms", 0);
                malformedActivity = params.value("malformed", false);
            } else if (method == "session_fixture_info") {
                std::lock_guard<std::mutex> lock(mutex);
                result["info_calls"] = infoCalls;
                result["sessions"] = sessions;
            } else if (method == "plm_fixture_descendant" || method == "session_fixture_descendant") {
                result["descendant"] = descendant(params.value("inherit_stdout", false));
            } else if (method == "agent_admin_turn") {
                {
                    std::lock_guard<std::mutex> lock(mutex);
                    active = params.at("run_id").get<std::string>();
                    cancelled = false;
                    ignoreCancel = params.value("ignore_cancel", false);
                    sessions.insert(params.at("session_id").get<std::string>());
                    steer.clear(); answers = Json::object();
                }
                if (params.value("spawn_descendant", false)) result["descendant"] = descendant();
                emit({{"id", id + 999}, {"event", "wrong"}, {"data", {{"wrong", true}}}});
                emit({{"id", id}, {"event", "start"}, {"data", result}});
                if (params.value("die", false)) TerminateProcess(GetCurrentProcess(), 23);
                std::unique_lock<std::mutex> lock(mutex);
                changed.wait(lock, [this] { return cancelled; });
                result["cancelled"] = true; result["steer"] = steer; result["answers"] = answers;
                active.clear();
            }
            emit({{"id", id}, {"ok", true}, {"result", result}});
        }
        // Fixture listener threads intentionally have process lifetime.
        std::_Exit(0);
    }
};

Json rpc(const std::string& method, const Json& params = Json::object(),
         const Json& ctx = Json::object()) {
    Json result; std::string error;
    const bool ok = Bridge::dispatch(method, params, result, error, 5000, {}, ctx);
    require(ok, method + ": " + error);
    return result;
}
struct Turn {
    std::string run, session;
    std::thread thread;
    std::mutex mutex;
    Json start = Json::object(), result;
    std::string error;
    bool ok = false;
    std::atomic<bool> finished{false};
    std::atomic<bool> wrongEvent{false};
    Turn(std::string runId, std::string sessionId, Json extra = Json::object())
        : run(std::move(runId)), session(std::move(sessionId)) {
        extra["run_id"] = run; extra["session_id"] = session;
        thread = std::thread([this, extra] {
            ok = Bridge::dispatch("agent_admin_turn", extra, result, error, -1,
                [this](const Json& event) {
                    std::lock_guard<std::mutex> lock(mutex);
                    if (event.contains("wrong")) wrongEvent = true;
                    else start = event;
                });
            finished = true;
        });
    }
    ~Turn() {
        if (thread.joinable()) { Bridge::shutdownIfCreated(); thread.join(); }
    }
    Json started() {
        eventually([this] { std::lock_guard<std::mutex> lock(mutex); return !start.empty() || finished.load(); },
                   "turn did not start: " + run);
        std::lock_guard<std::mutex> lock(mutex);
        require(!start.empty(), "turn failed before streaming: " + error);
        require(!wrongEvent, "wrong request notification delivered");
        return start;
    }
    bool hasStarted() { std::lock_guard<std::mutex> lock(mutex); return !start.empty(); }
    Json control(const std::string& action, Json params = Json::object(), bool expected = true) {
        params["run_id"] = run;
        Json value; std::string err; bool matched = false;
        const bool success = Bridge::controlActiveRun(action, params, matched, value, err);
        require(matched, "active control was not matched");
        require(success == expected, "unexpected control outcome: " + action + ": " + err);
        return value;
    }
    void cancel() {
        control("cancel");
        eventually([this] { return finished.load(); }, "cancel did not end turn");
        thread.join();
        require(ok && result.value("cancelled", false), "turn cancellation failed: " + error);
    }
};
void ageExtras() {
    auto& pool = Bridge::pool();
    std::lock_guard<std::mutex> lock(pool.mutex);
    for (auto* extra : pool.extras)
        pool.slots.at(extra).lastEnded = Clock::now() - std::chrono::minutes(11);
}
void initialize() {
    const auto path = std::filesystem::u8path(chudware::platform::executablePath()).wstring();
    require(_wputenv_s(L"CHUDWARE_WORKER_BIN", path.c_str()) == 0, "worker override failed");
    require(_wputenv_s(L"CHUDWARE_AGENT_WORKERS", L"4") == 0, "worker pool override failed");
}

void outputIoCase() {
    auto& lane = Bridge::general();
    const auto original = rpc("plm_fixture");
    void* const event = lane.stdoutEvent_;
    require(event != nullptr, "stdout completion event was not created");
    const std::string payload(32768, 'f');
    Json result; std::string error;
    const auto before = Clock::now();
    require(!lane.call("plm_fixture_split", {{"payload", payload}, {"fragments", 6}, {"delay_ms", 150}},
                       result, error, 220) && error == "timeout waiting for response",
            "fragmented response did not reach its absolute read deadline");
    require(Clock::now() - before < std::chrono::milliseconds(650),
            "read deadline restarted after each received fragment");
    {
        std::unique_lock<std::timed_mutex> lock(lane.mtx_);
        std::string completed;
        require(lane.readLine(completed, 2000), "timed-out OVERLAPPED read lost its remaining frame");
        require(Json::parse(completed).at("result").at("payload") == payload,
                "cancellation completion lost partial response bytes");
        // Exercise repeated zero-deadline cancellation and reuse of the same
        // event, OVERLAPPED stack storage and pipe without a reader thread.
        for (int i = 0; i < 16; ++i)
            require(!lane.readLine(completed, 0), "idle zero-deadline read unexpectedly produced a frame");
    }
    require(rpc("plm_fixture")["pid"] == original["pid"] && lane.stdoutEvent_ == event,
            "bounded reads replaced the worker or allocated a new completion event");
    const auto final = rpc("plm_fixture_split",
        {{"payload", payload}, {"fragments", 3}, {"exit", true}});
    require(final["payload"] == payload, "process-exit notification overtook final buffered stdout");
    rpc("plm_fixture");  // ordinary RPC replaces the exited generation, not the lane event
    require(lane.stdoutEvent_ == event, "worker replacement discarded its reusable stdout event");
    const auto inherited = rpc("plm_fixture_descendant", {{"inherit_stdout", true}});
    {
        std::unique_lock<std::timed_mutex> lock(lane.mtx_);
        require(!lane.callOnce("plm_fixture_split",
            {{"payload", payload}, {"fragments", 3}, {"partial_eof", true}, {"exit", true}},
            result, error, 2000, {}, Json::object()) && error == "closed waiting for response",
            "unterminated final payload did not report worker death");
        lane.reap();
    }
    eventually([&] { return !alive(inherited.at("descendant").get<DWORD>()); },
               "a descendant-held writer prevented EOF after root worker death");
    rpc("plm_fixture");
    require(lane.stdoutEvent_ == event, "EOF cleanup broke completion event reuse");
}

void standardCase() {
    initialize();
    auto census = Bridge::activity();
    require(census.busy == 0 && census.background == 0 && census.unknown == 0,
            "unused bridges are not idle");
    require(!Bridge::agentCreated_ && !Bridge::generalCreated_, "activity eagerly created worker lanes");
    Json refused; std::string error;
    require(!Bridge::dispatch("plm_fixture", {}, refused, error, 1000, {}, {{"cell", true}}) &&
            refused.value("code", "") == "cell_refused", "Windows cell was not explicitly refused");
    const auto general = rpc("plm_fixture");
    const auto primary = rpc("session_fixture");
    require(general["pid"] != primary["pid"] && general["lane"] == "general" &&
            primary["lane"] == "agent" && general["engine"] == "1", "lane/environment isolation failed");
    const std::string large(256 * 1024, 'x');
    require(rpc("plm_fixture_large", {{"payload", large}})["payload"] == large,
            "multi-chunk stdio request/response was truncated");
    {
        Turn first("first", "chat-a");
        const auto firstInfo = first.started();
        Turn second("second", "chat-b");
        const auto secondInfo = second.started();
        require(firstInfo["pid"] != secondInfo["pid"] && secondInfo["lane"] == "agent-turn",
                "concurrent chats do not have independent agent-turn workers");
        census = Bridge::activity();
        require(census.busy == 2 && census.unknown == 0, "active turn census is not authoritative");
        const auto before = Clock::now();
        require(!Bridge::dispatch("subscription_status", {}, refused, error, 5000, {}) &&
                refused.value("code", "") == "worker_lane_busy" &&
                Clock::now() - before < std::chrono::seconds(1), "busy stdio call did not fail fast");
        require(rpc("plm_fixture")["pid"] == general["pid"], "turn blocked/replaced general lane");
        for (const auto* failure : {"closed", "malformed", "refused", "oversized", "timeout"}) {
            first.control("steer", {{"text", "not delivered"}, {"_fail", failure}}, false);
            require(first.control("status").value("active", false), "control generation lost after failure");
        }
        first.control("steer", {{"text", "kept"}});
        first.control("answer", {{"answers", {{"choice", "yes"}}}});
        Turn same("same-chat", "chat-a");
        Sleep(150);
        require(!same.hasStarted(), "same conversation ran concurrently");
        first.cancel();
        require(first.result["steer"] == "kept" && first.result["answers"]["choice"] == "yes",
                "steer/answer did not reach active run");
        require(same.started()["pid"] == firstInfo["pid"], "same chat lost its worker ownership");
        same.cancel(); second.cancel();
        const auto info = rpc("session_fixture_info", {{"session_id", "chat-a"}});
        require(info["info_calls"] == 1, "control failures re-fetched credentials through stdio");
    }
    // Refused releases keep the exact owner, then a successful release migrates.
    rpc("session_fixture_release", {{"session_id", "chat-a"}, {"refuse", true}});
    {
        Turn busy("busy-owner", "busy-chat");
        const auto owner = busy.started();
        Turn waiting("waiting-owner", "chat-a");
        Sleep(150); require(!waiting.hasStarted(), "refused release forked conversation ownership");
        busy.cancel();
        require(waiting.started()["pid"] == owner["pid"], "refused release moved conversation");
        waiting.cancel();
    }
    rpc("session_fixture_release", {{"session_id", "chat-a"}, {"refuse", false}});
    {
        Turn busy("busy-migrate", "busy-chat");
        const auto owner = busy.started();
        Turn migrated("migrated", "chat-a");
        require(migrated.started()["pid"] != owner["pid"], "released idle session did not migrate");
        migrated.cancel(); busy.cancel();
        const auto old = rpc("session_fixture_info", {{"session_id", "busy-chat"}});
        require(std::find(old["sessions"].begin(), old["sessions"].end(), "chat-a") == old["sessions"].end(),
                "old worker retained the released session");
    }
    const Json idle{{"ok", true}, {"complete", true}, {"active", 0}, {"by_kind", Json::object()}};
    rpc("plm_fixture_activity", {{"value", {{"ok", true}, {"complete", true}, {"active", 2},
                                            {"by_kind", {{"sdk", 2}}}}}});
    census = Bridge::activity();
    require(census.background == 2 && census.unknown == 0, "background census lost work");
    rpc("plm_fixture_activity", {{"value", idle}, {"malformed", true}});
    require(Bridge::activity().unknown == 1, "malformed census was reported as idle");
    rpc("plm_fixture_activity", {{"value", idle}, {"delay_ms", 2400}});
    require(Bridge::activity().unknown >= 1, "census timeout was reported as idle");
    Sleep(600);
    require(rpc("plm_fixture_activity", {{"value", idle}})["pid"] == general["pid"],
            "bounded census failure replaced a live worker");
    require(Bridge::activity().unknown == 0, "late census poisoned subsequent replies");
    const auto backgroundOwner = rpc("session_fixture_activity",
        {{"session_id", "chat-a"}, {"value", {{"ok", true}, {"complete", true}, {"active", 1},
                                             {"by_kind", {{"sdk", 1}}}}}});
    ageExtras(); Bridge::reapIdleExtras(Bridge::pool());
    require(alive(backgroundOwner.at("pid").get<DWORD>()), "reaper killed authoritative background work");
    auto unknown = idle; unknown["complete"] = false;
    rpc("session_fixture_activity", {{"session_id", "chat-a"}, {"value", unknown}});
    ageExtras(); Bridge::reapIdleExtras(Bridge::pool());
    require(alive(backgroundOwner.at("pid").get<DWORD>()), "reaper treated unknown work as idle");
    rpc("session_fixture_activity", {{"session_id", "chat-a"}, {"value", idle}});
    std::vector<long> extraPids;
    for (auto* extra : Bridge::pool().extras) extraPids.push_back(extra->livePid_.load());
    ageExtras(); Bridge::reapIdleExtras(Bridge::pool());
    for (const auto pid : extraPids) if (pid > 0) require(!alive(static_cast<DWORD>(pid)), "idle extra survived reaping");
    outputIoCase();
    // Worker death after a streamed start is never replayed, and kills its descendants.
    const auto ownedChild = rpc("session_fixture_descendant", {{"session_id", "busy-chat"}});
    {
        Turn lost("lost", "busy-chat", {{"die", true}});
        lost.started();
        eventually([&] { return lost.finished.load(); }, "dead worker left turn hanging");
        lost.thread.join();
        require(!lost.ok && lost.result.value("code", "") == "worker_lost_mid_turn", "lost turn was replayed");
        eventually([&] { return !alive(ownedChild.at("descendant").get<DWORD>()); }, "worker descendant outlived death");
    }
    {
        Turn ending("ending", "end-chat"); ending.started();
        Bridge::shutdownIfCreated();
        eventually([&] { return ending.finished.load(); }, "shutdown did not cancel active turn");
        ending.thread.join(); require(ending.ok, "cooperative shutdown lost turn terminal");
    }
    require(!Bridge::dispatch("plm_fixture", {}, refused, error, 1000, {}), "shutdown respawned worker");
}
void forcedCase() {
    initialize();
    Turn blocked("forced", "forced-chat", {{"ignore_cancel", true}, {"spawn_descendant", true}});
    const auto info = blocked.started();
    const auto before = Clock::now();
    Bridge::shutdownIfCreated();
    require(Clock::now() - before < std::chrono::seconds(15), "forced shutdown exceeded bounded grace");
    eventually([&] { return blocked.finished.load(); }, "forced shutdown did not unblock stdio");
    blocked.thread.join();
    require(!blocked.ok && blocked.result.value("code", "") == "worker_lost_mid_turn", "forced turn restarted");
    // Job termination is asynchronous; observe completion as in the worker-death case.
    eventually([&] {
        return !alive(info.at("pid").get<DWORD>()) && !alive(info.at("descendant").get<DWORD>());
    }, "forced shutdown left process tree alive");
}
void crashOwnerCase(const std::filesystem::path& resultPath) {
    initialize();
    const auto result = rpc("plm_fixture_descendant");
    std::ofstream file(resultPath); file << result.dump(); file.close();
    // No normal bridge teardown: prove kernel job ownership on hostd death.
    TerminateProcess(GetCurrentProcess(), 0);
}
void runCase(const std::wstring& args, int deadlineMs = 30000,
             const std::filesystem::path& executable = {}) {
    const auto diagnostic = std::filesystem::temp_directory_path() /
        ("chudware-worker-case-" + std::to_string(GetCurrentProcessId()) + ".log");
    auto child = startSelf(args + L" --diagnostic \"" + diagnostic.wstring() + L"\"", executable);
    const DWORD waited = WaitForSingleObject(child.hProcess, deadlineMs);
    if (waited != WAIT_OBJECT_0) TerminateProcess(child.hProcess, 99);
    DWORD code = 0; GetExitCodeProcess(child.hProcess, &code); CloseHandle(child.hProcess);
    std::string detail;
    { std::ifstream file(diagnostic); detail.assign(std::istreambuf_iterator<char>(file), {}); }
    std::filesystem::remove(diagnostic);
    require(waited == WAIT_OBJECT_0 && code == 0,
            "native regression subprocess " + hostd::nativeUtf8(args) +
            " failed: exit=" + std::to_string(code) + ", wait=" + std::to_string(waited) +
            (detail.empty() ? "" : ": " + detail));
}
}
int wmain(int argc, wchar_t** argv) {
    std::filesystem::path diagnostic;
    for (int i = 1; i + 1 < argc; ++i)
        if (std::wstring(argv[i]) == L"--diagnostic") diagnostic = argv[++i];
    try {
        if (argc > 1 && std::wstring(argv[1]) == L"--module") return Fixture{}.run();
        if (argc > 1 && std::wstring(argv[1]) == L"--descendant") { Sleep(INFINITE); return 0; }
        if (argc > 2 && std::wstring(argv[1]) == L"--case") {
            const std::wstring which = argv[2];
            if (which == L"standard") standardCase();
            else if (which == L"forced") forcedCase();
            else if (which == L"stdio") { initialize(); outputIoCase(); Bridge::shutdownIfCreated(); }
            else if (which == L"crash-owner" && argc > 3) crashOwnerCase(argv[3]);
            else throw std::runtime_error("unknown native regression case");
            return 0;
        }
        const auto directory = std::filesystem::temp_directory_path() /
            (L"Chudware worker bridge \u03bb " + std::to_wstring(GetCurrentProcessId()));
        std::filesystem::create_directories(directory);
        // Exercise compiled-worker discovery/launch from spaces and non-ASCII paths.
        const auto relocated = directory / L"native worker \u03bb.exe";
        std::filesystem::copy_file(std::filesystem::u8path(chudware::platform::executablePath()), relocated);
        runCase(L"--case standard", 30000, relocated);
        runCase(L"--case forced", 30000, relocated);
        const auto resultPath = directory / "owned-tree.json";
        runCase(L"--case crash-owner \"" + resultPath.wstring() + L"\"");
        Json owned;
        { std::ifstream file(resultPath); file >> owned; }
        eventually([&] { return !alive(owned.at("pid").get<DWORD>()) &&
                                !alive(owned.at("descendant").get<DWORD>()); },
                   "worker process tree survived abrupt owner death");
        std::filesystem::remove(resultPath); std::filesystem::remove(relocated);
        std::filesystem::remove(directory);
        std::cout << "PASS native Windows WorkerBridge lanes/control/census/ownership/shutdown\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL native Windows WorkerBridge: " << error.what() << '\n';
        if (!diagnostic.empty()) {
            std::ofstream file(diagnostic);
            file << error.what() << '\n';
        }
        Bridge::shutdownIfCreated();
        return 1;
    }
}
#else
#error This regression driver requires native Windows; Wine/cross-compilation cannot qualify it.
#endif
