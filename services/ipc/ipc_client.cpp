#include "ipc_client.h"

#include <chrono>
#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <filesystem>
#include <map>
#include <string_view>
#include <thread>
#include <utility>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <process.h>  // _getpid
#else
#include <csignal>
#ifndef __EMSCRIPTEN__
#include <fcntl.h>
#include <poll.h>
#include <spawn.h>
#include <sys/wait.h>
#endif
#include <unistd.h>
#endif

#ifdef __EMSCRIPTEN__
#include <emscripten.h>            // emscripten_sleep
#include <emscripten/threading.h>  // emscripten_is_main_runtime_thread
#endif

#include "ipc_discovery.h"

#if !defined(_WIN32) && !defined(__EMSCRIPTEN__)
extern char** environ;
#endif
namespace {
long selfPid() {
#ifdef _WIN32
    return (long)_getpid();
#else
    return (long)::getpid();
#endif
}


bool connectCancelled(const std::atomic<bool>* cancelled) {
    return cancelled && cancelled->load(std::memory_order_acquire);
}

bool connectDelay(int milliseconds, const std::atomic<bool>* cancelled) {
    const auto until = std::chrono::steady_clock::now() +
                       std::chrono::milliseconds(milliseconds);
    while (!connectCancelled(cancelled)) {
        const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
            until - std::chrono::steady_clock::now()).count();
        if (left <= 0) return true;
        std::this_thread::sleep_for(std::chrono::milliseconds(std::min<long long>(left, 10)));
    }
    return false;
}

#ifndef __EMSCRIPTEN__
// Probe only our own short-lived --version process. A broken executable must
// neither pin connection startup nor leave GUI shutdown waiting in fgets/pclose.
std::string versionOutput(const std::string& bin, const std::atomic<bool>* cancelled) {
    if (connectCancelled(cancelled)) return {};
    std::string out;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
#ifdef _WIN32
    SECURITY_ATTRIBUTES security{sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE};
    HANDLE input = nullptr, output = nullptr;
    if (!CreatePipe(&input, &output, &security, 0)) return {};
    SetHandleInformation(input, HANDLE_FLAG_INHERIT, 0);
    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    startup.hStdOutput = startup.hStdError = output;
    PROCESS_INFORMATION process{};
    std::wstring command = L"\"" + std::filesystem::u8path(bin).wstring() + L"\" --version";
    const bool started = CreateProcessW(nullptr, command.data(), nullptr, nullptr, TRUE,
                                        CREATE_NO_WINDOW, nullptr, nullptr, &startup, &process);
    CloseHandle(output);
    if (!started) { CloseHandle(input); return {}; }
    CloseHandle(process.hThread);
    bool exited = false;
    for (;;) {
        DWORD available = 0;
        if (PeekNamedPipe(input, nullptr, 0, nullptr, &available, nullptr) && available) {
            char buffer[256];
            DWORD got = 0;
            if (ReadFile(input, buffer, std::min<DWORD>(available, sizeof(buffer)), &got, nullptr))
                out.append(buffer, std::min<size_t>(got, 256 - out.size()));
        }
        if (exited) break;  // drain final output before finishing a reaped child
        exited = WaitForSingleObject(process.hProcess, 20) == WAIT_OBJECT_0;
        if (connectCancelled(cancelled) || std::chrono::steady_clock::now() >= deadline)
            break;
    }
    if (!exited) {
        TerminateProcess(process.hProcess, 1);
        WaitForSingleObject(process.hProcess, 2000);
        out.clear();
    }
    CloseHandle(process.hProcess);
    CloseHandle(input);
#else
    int pipe[2];
    if (::pipe(pipe) != 0) return {};
    ::fcntl(pipe[0], F_SETFD, FD_CLOEXEC);
    ::fcntl(pipe[1], F_SETFD, FD_CLOEXEC);
    ::fcntl(pipe[0], F_SETFL, O_NONBLOCK);
    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_adddup2(&actions, pipe[1], STDOUT_FILENO);
    posix_spawn_file_actions_addopen(&actions, STDERR_FILENO, "/dev/null", O_WRONLY, 0);
    posix_spawn_file_actions_addclose(&actions, pipe[0]);
    posix_spawn_file_actions_addclose(&actions, pipe[1]);
    char* args[] = {const_cast<char*>(bin.c_str()), const_cast<char*>("--version"), nullptr};
    pid_t pid = -1;
    const int spawned = ::posix_spawn(&pid, bin.c_str(), &actions, nullptr, args, ::environ);
    posix_spawn_file_actions_destroy(&actions);
    ::close(pipe[1]);
    if (spawned != 0) { ::close(pipe[0]); return {}; }
    bool exited = false;
    for (;;) {
        char buffer[256];
        const ssize_t got = ::read(pipe[0], buffer, sizeof(buffer));
        if (got > 0) out.append(buffer, std::min<size_t>(size_t(got), 256 - out.size()));
        if (exited) break;
        int status = 0;
        const pid_t reaped = ::waitpid(pid, &status, WNOHANG);
        exited = reaped == pid || (reaped < 0 && errno != EINTR);
        if (connectCancelled(cancelled) || std::chrono::steady_clock::now() >= deadline)
            break;
        if (!exited) ::poll(nullptr, 0, 20);
    }
    if (!exited) {
        ::kill(pid, SIGKILL);
        while (::waitpid(pid, nullptr, 0) < 0 && errno == EINTR) {}
        out.clear();
    }
    ::close(pipe[0]);
#endif
    return connectCancelled(cancelled) ? std::string() : out;
}
#endif

// Build id of the hostd binary ON DISK, compared to hello.git_rev for a
// non-destructive warning. Cache by path/mtime to avoid repeat subprocesses.
std::string diskBuildId(const std::string& bin, const std::atomic<bool>* cancelled) {
#ifdef __EMSCRIPTEN__
    (void)bin;
    (void)cancelled;
    return "";  // no hostd binary / no popen in the browser
#else
    if (bin.empty()) return "";
    std::error_code ec;
    const auto t = std::filesystem::last_write_time(bin, ec);
    const long long mtime =
        ec ? 0
           : std::chrono::duration_cast<std::chrono::milliseconds>(t.time_since_epoch())
                 .count();
    static std::mutex m;
    static std::map<std::string, std::pair<long long, std::string>> cache;
    {
        std::lock_guard<std::mutex> lk(m);
        auto it = cache.find(bin);
        if (it != cache.end() && it->second.first == mtime) return it->second.second;
    }
    std::string out = versionOutput(bin, cancelled);
    if (connectCancelled(cancelled)) return {};
    const auto newline = out.find_first_of("\r\n");
    if (newline != std::string::npos) out.resize(newline);
    while (!out.empty() && (out.back() == '\n' || out.back() == '\r' || out.back() == ' '))
        out.pop_back();
    const size_t sp = out.find_last_of(" \t");
    if (sp != std::string::npos) out = out.substr(sp + 1);
    std::lock_guard<std::mutex> lk(m);
    cache[bin] = {mtime, out};
    return out;
#endif  // !__EMSCRIPTEN__
}

// Runs `f` when the scope ends, on every return path.
template <class F>
struct OnExit {
    F f;
    ~OnExit() { f(); }
};
template <class F>
OnExit<F> onExit(F f) {
    return OnExit<F>{std::move(f)};
}

// Inspect only object member boundaries, without allocating a document tree.
// Unusual/escaped member names fall back to the ordinary JSON parser.
std::string_view rawMember(std::string_view object, std::string_view wanted) {
    // hostd's compact envelopes put data first and event last. Recognizing
    // that shape avoids walking the (potentially megabyte) tree just to find
    // its envelope fields; arbitrary JSON layouts use the scanner below.
    if (object.size() >= 12 && object.substr(0, 8) == "{\"data\":" &&
        object.back() == '}' && object[object.size() - 2] == '"') {
        const size_t event = object.rfind(",\"event\":\"");
        if (event != std::string_view::npos &&
            object.substr(event + 10, object.size() - event - 12).find_first_of("\"\\") ==
                std::string_view::npos) {
            if (wanted == "data") return object.substr(8, event - 8);
            if (wanted == "event") return object.substr(event + 9, object.size() - event - 10);
        }
    }
    size_t p = 0;
    const auto space = [&] { while (p < object.size() &&
        (object[p] == ' ' || object[p] == '\n' || object[p] == '\r' || object[p] == '\t')) ++p; };
    const auto stringEnd = [&](size_t start) {
        size_t end = start + 1;
        while (end < object.size()) {
            if (object[end] == '\\') { end += 2; continue; }
            if (object[end++] == '"') return end;
        }
        return object.size();
    };
    space();
    if (p == object.size() || object[p++] != '{') return {};
    for (;;) {
        space();
        if (p >= object.size() || object[p] != '"') return {};
        const size_t key = p++;
        p = stringEnd(key);
        const auto name = object.substr(key + 1, p - key - 2);
        space();
        if (p >= object.size() || object[p++] != ':') return {};
        space();
        const size_t start = p;
        int depth = 0;
        while (p < object.size()) {
            const char c = object[p];
            if (c == '"') { p = stringEnd(p); continue; }
            if (c == '{' || c == '[') ++depth;
            else if (c == '}' || c == ']') {
                if (depth == 0) break;
                --depth;
            } else if (c == ',' && depth == 0) break;
            ++p;
        }
        if (name == wanted) {
            size_t end = p;
            while (end > start && (object[end - 1] == ' ' || object[end - 1] == '\n' ||
                   object[end - 1] == '\r' || object[end - 1] == '\t')) --end;
            return object.substr(start, end - start);
        }
        if (p >= object.size() || object[p++] != ',') return {};
    }
}

}  // namespace

namespace chudware_ipc {

using nlohmann::json;

bool EngineClient::usesBackgroundConnection(const std::string& method,
                                            const json& params) const {
    if (clientKind_ != "gui" || std::this_thread::get_id() == primaryThread_ ||
        (params.is_object() && params.contains("doc_id")))
        return false;
    // Server::clientLoop dispatches synchronously: sharing one socket would still
    // queue the GUI behind cold worker/provider calls even without a client mutex.
    // These exact names are document-free catalogs/status (worker_rpc.py's
    // subscription, OMP, skills and prompt handlers; fw_projects/status and
    // engine capability queries). Do NOT generalize to prefixes: unknown methods,
    // mints, document reads/edits and all registry/hold/subscription RPCs retain the
    // primary client identity, including its ro/CAS and edit-hold semantics.
    // omp_login is the one long call here (a whole browser OAuth flow, minutes): on the
    // primary socket it would park every other primary-socket call for that long.
    static constexpr const char* queries[] = {
        "ping", "daemon_status", "worker_control_info", "agent_list_models",
        "subscription_accounts", "subscription_status", "subscription_usage",
        "omp_provider_list", "omp_auth_state", "omp_logout_accounts", "omp_models_list",
        "omp_login", "omp_session_stats", "skills_list", "skills_get",
        "ai_prompt_baseline", "ai_tool_catalog", "fw_projects", "fw_status",
        "em_status", "sim_caps", "topology_capabilities", "topology_dfam"
    };
    for (const char* name : queries)
        if (method == name) return true;
    return false;
}

EngineClient* EngineClient::backgroundClient() {
    std::lock_guard<std::mutex> lk(backgroundMtx_);
    auto& slot = backgroundClients_[std::this_thread::get_id()];
    if (!slot) {
        slot = std::make_unique<EngineClient>();
        slot->sockPath_ = sockPath_;
        slot->clientKind_ = "gui-background";
        slot->userId_ = userId_;
        slot->userDisplay_ = userDisplay_;
        // The primary socket is the GUI's event subscriber. Hostd also broadcasts
        // on private sockets; retaining those duplicates would leak an idle queue.
        slot->discardEvents_ = true;
    }
    // No I/O under backgroundMtx_: each calling thread connects/handshakes/replies
    // on its own socket and mutex. A side EOF/timeout cannot invalidate the primary
    // socket, advance its reconnectGeneration or change the unreachable chip.
    // gui-background never pins an idle daemon like gui (Server::pinningClients).
    return slot.get();
}

void EngineClient::pollBackgroundClients() {
    std::unique_lock<std::mutex> lk(backgroundMtx_, std::try_to_lock);
    if (!lk.owns_lock()) return;
    for (const auto& entry : backgroundClients_) entry.second->pollAlive();
}

void EngineClient::closeConnection() {
    std::lock_guard<std::mutex> lk(socketMtx_);
    connected_.store(false, std::memory_order_release);
    conn_.close();
}

void EngineClient::abortPending() {
    abort_.store(true, std::memory_order_release);
    connected_.store(false, std::memory_order_release);
    {
        // shutdown wakes an active read without modifying its read buffer/handle.
        // The read owner closes it; idle sockets can be closed here immediately.
        std::lock_guard<std::mutex> lk(socketMtx_);
        conn_.shutdown();
    }
    {
        std::unique_lock<std::mutex> lk(mtx_, std::try_to_lock);
        if (lk.owns_lock()) closeConnection();
    }
    std::lock_guard<std::mutex> lk(backgroundMtx_);
    for (const auto& entry : backgroundClients_) entry.second->abortPending();
}

void EngineClient::disconnect() {
    // Drop the entire logical GUI client, not just its primary/event connection.
    abortPending();
}

bool EngineClient::tryConnect(std::string& err, const std::atomic<bool>* cancelled) {
    std::lock_guard<std::mutex> lk(socketMtx_);
    if (cancelled && cancelled->load(std::memory_order_acquire)) {
        err = "aborted";
        return false;
    }
    conn_ = connectTo(sockPath_, err, cancelled);
    if (cancelled && cancelled->load(std::memory_order_acquire)) {
        conn_.close();
        connected_.store(false, std::memory_order_release);
        err = "aborted";
        return false;
    }
    connected_.store(conn_.valid(), std::memory_order_release);
    if (conn_.valid()) {
        // Fresh socket after abortPending() — allow subsequent call()s again.
        abort_.store(false, std::memory_order_release);
        return true;
    }
    return false;
}

json EngineClient::helloError() const {
    std::lock_guard<std::mutex> lk(helloErrMtx_);
    return helloError_;
}

void EngineClient::setHelloError(json error) {
    std::lock_guard<std::mutex> lk(helloErrMtx_);
    helloError_ = std::move(error);
}

// A refused hello closes the connection (Server::admitWhileDraining). Name its
// code, message and hint in the same form as daemon_bridge.py.
void EngineClient::refuseHello(const json& error, std::string& err) {
    closeConnection();
    const auto text = [&error](const char* key) {
        const auto it = error.find(key);
        return it != error.end() && it->is_string() ? it->get<std::string>() : std::string();
    };
    const std::string code = text("code");
    const std::string message = text("message");
    const std::string hint = text("hint");
    err = "hostd refused hello (" + (code.empty() ? std::string("error") : code) + ")";
    if (!message.empty()) err += ": " + message;
    if (!hint.empty()) err += " " + hint;
}

bool EngineClient::hello(const std::string& kind, std::string& err, json& refusal,
                         const std::atomic<bool>* cancelled) {
    json params = {{"client", kind},
                   {"pid", selfPid()},
                   {"proto", 1}};
    // Optional multi-user identity — only when the client set Options.userId.
    if (!userId_.empty())
        params["user"] = {{"id", userId_}, {"display", userDisplay_}};
    json result;
    // Bounded on every platform: a daemon that accepts but never answers hello must
    // fail the connect (callers fall back or retry) rather than wedge startup. Same
    // bound as reconnectLocked's inline hello; hostd answers hello even mid-solve.
    // Browser: never block first paint forever if the gateway is slow/silent —
    // maintainConnection retries with backoff when hello fails.
    const int timeoutMs = (kind == "control") ? 2000 : 5000;
    if (!call("hello", params, result, err, timeoutMs, {}, cancelled)) {
        // An error object is a refusal by the daemon; anything else failed on the transport.
        if (result.is_object()) {
            refusal = std::move(result);
            refuseHello(refusal, err);
        }
        return false;
    }
    serverInfo_ = result;
    return true;
}

bool EngineClient::connectOrSpawn(const Options& opts, std::string& err) {
    if (connectCancelled(opts.cancelled)) { err = "aborted"; return false; }
    if (!connectInner(opts, err)) return false;
    if (!opts.allowSpawn) return true;  // control clients (--stop/--status) never drain

    // A version mismatch is not authority to stop a daemon: another GUI can
    // have unsaved documents or a worker can be between per-turn connections.
    // Hostd's own binary watcher performs the soft drain once it detects change.
    const std::string running = serverInfo_.value("git_rev", std::string());
    const std::string disk = diskBuildId(resolveHostdBin(opts.hostdBin), opts.cancelled);
    if (connectCancelled(opts.cancelled)) { err = "aborted"; return false; }
    if (!running.empty() && !disk.empty() && running != "unknown" && disk != "unknown" &&
        running != disk) {
        static std::mutex warningMutex;
        static std::pair<std::string, std::string> lastWarning;
        std::lock_guard<std::mutex> lock(warningMutex);
        if (lastWarning != std::make_pair(running, disk)) {
            lastWarning = {running, disk};
            std::fprintf(stderr,
                         "[ipc] hostd on disk is %s, running %s; continuing existing work "
                         "until the daemon's soft stale-binary drain\n",
                         disk.c_str(), running.c_str());
        }
    }
    return true;
}

bool EngineClient::connectInner(const Options& opts, std::string& err) {
    if (connectCancelled(opts.cancelled)) { err = "aborted"; return false; }
    // Publish this attempt's hello error (null unless the daemon refused) when it ends.
    json refusal;
    const auto publish = onExit([&] { setHelloError(std::move(refusal)); });
#ifdef CHUDWARE_DEVELOPER_BUILD
    const std::string keyBin = opts.hostdBin;
#else
    // Installed clients and hostd must derive the rendezvous key from the same
    // verified executable, even when the caller supplied no hint.
    const std::string keyBin = resolveHostdBin(opts.hostdBin);
    if (keyBin.empty()) {
        err = "canonical manifest-owned hostd binary unavailable";
        return false;
    }
#endif
    const std::string key = instanceKey(keyBin);
    sockPath_ = socketPath(key);
    clientKind_ = opts.clientKind;
    primaryThread_ = opts.primaryThread == std::thread::id{}
        ? std::this_thread::get_id() : opts.primaryThread;
    userId_ = opts.userId;
    userDisplay_ = opts.userDisplay;
    discardEvents_ = opts.discardUnconsumedEvents;
    if (sockPath_.empty()) {
        err = "no runtime dir for hostd socket";
        return false;
    }
    std::string firstErr;
    if (tryConnect(firstErr, opts.cancelled))
        return hello(opts.clientKind, err, refusal, opts.cancelled);
    if (connectCancelled(opts.cancelled)) { err = "aborted"; return false; }

    if (!opts.allowSpawn) {
        err = "hostd not running (" + firstErr + ")";
        return false;
    }
    const std::string bin = resolveHostdBin(opts.hostdBin);
    if (bin.empty()) {
#ifdef CHUDWARE_DEVELOPER_BUILD
        err = "hostd binary not found (set CHUDWARE_HOSTD_BIN); connect failed: " + firstErr;
#else
        err = "canonical manifest-owned hostd binary unavailable; connect failed: " + firstErr;
#endif
        return false;
    }
#ifndef CHUDWARE_DEVELOPER_BUILD
    // Re-verify immediately before CreateProcess/exec. resolveHostdBin also verifies,
    // but this closes the discovery-to-spawn swap window for ordinary replacements.
    const std::string rechecked = resolveHostdBin(opts.hostdBin);
    if (rechecked.empty() || rechecked != bin) {
        err = "hostd binary changed after integrity verification";
        return false;
    }
#endif
    if (connectCancelled(opts.cancelled)) { err = "aborted"; return false; }
    if (!spawnDetached(bin, "", err, opts.cancelled)) return false;

    // Retry-connect: 50 ms doubling capped at 500 ms, until the budget runs out.
    // The daemon binds BEFORE loading the engine, so this only needs to cover
    // process start + bind, not the (slow) OCCT load.
    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::milliseconds(opts.spawnBudgetMs);
    int backoff = 50;
    for (;;) {
        std::string e;
        if (tryConnect(e, opts.cancelled))
            return hello(opts.clientKind, err, refusal, opts.cancelled);
        if (connectCancelled(opts.cancelled)) { err = "aborted"; return false; }
        if (std::chrono::steady_clock::now() >= deadline) {
            err = "hostd spawn timed out: " + e;
            return false;
        }
        if (!connectDelay(backoff, opts.cancelled)) { err = "aborted"; return false; }
        backoff = std::min(backoff * 2, 500);
    }
}

void EngineClient::retainEventLine(std::string&& line) {
    const auto event = rawMember(line, "event");
    if (discardEvents_ || event == "\"daemon.draining\"") return;
    if (event.empty()) {
        // Escaped keys/unusual envelopes are valid JSON too: do not drop a push
        // merely because the cheap peek cannot classify it.
        json msg = json::parse(line, nullptr, false);
        if (!msg.is_discarded() && msg.contains("event")) retainEvent(std::move(msg));
        return;
    }
    events_.push_back({std::move(line), {}});
    pendingEvents_.store(true, std::memory_order_release);
}

void EngineClient::retainEvent(json&& msg) {
    const auto ev = msg.find("event");
    if (ev == msg.end() || !ev->is_string() || *ev == "daemon.draining" || discardEvents_)
        return;
    events_.push_back({{}, std::move(msg)});
    pendingEvents_.store(true, std::memory_order_release);
}

void EngineClient::pollEventsLocked() {
    pollLimited_ = false;
    std::string line;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(1);
    size_t count = 0;
    while (conn_.valid() && conn_.readLine(line, 0)) {
        if (!discardEvents_) retainEventLine(std::move(line));
        // Reading stays bounded even when a writer continuously publishes.
        if (++count >= 256 || std::chrono::steady_clock::now() >= deadline) {
            pollLimited_ = true;
            break;
        }
    }
}

bool EngineClient::readResponse(int id, json& out, std::string& err, int timeoutMs,
                                const std::function<void(const json&)>& onEvent,
                                const std::atomic<bool>* cancelled) {
    // One deadline for the WHOLE response wait: event lines arriving on the way
    // must not restart the clock.
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::milliseconds(timeoutMs >= 0 ? timeoutMs : 0);
    std::string line;
    for (;;) {
        if (abort_.load(std::memory_order_acquire) ||
            (cancelled && cancelled->load(std::memory_order_acquire))) {
            err = "aborted";
            closeConnection();
            return false;
        }
        int wait = -1;
        if (timeoutMs >= 0) {
            const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
                                  deadline - std::chrono::steady_clock::now())
                                  .count();
            wait = left > 0 ? (int)left : 0;
        } else {
            // Infinite timeout: still wake periodically so abortPending is observed.
            wait = 200;
        }
        if (cancelled) wait = wait < 0 ? 50 : std::min(wait, 50);
        if (!conn_.readLine(line, wait)) {
            // abortPending() half-closes the socket, which reads as EOF: report the abort.
            if (abort_.load(std::memory_order_acquire) ||
                (cancelled && cancelled->load(std::memory_order_acquire))) continue;
            if (conn_.eof() || !conn_.valid()) {
                closeConnection();
                err = "daemon connection closed";
                return false;
            }
            // A poll slice expired with the peer alive. An unbounded call keeps waiting:
            // a solver factorizing a large system legitimately pushes nothing for minutes,
            // and failing here abandoned a running native solve after 200 ms of silence.
            if (timeoutMs < 0 || std::chrono::steady_clock::now() < deadline) continue;
            // A bounded call has reached its deadline with the response still in
            // flight, so this connection is mid-stream and unusable — close it; the
            // next call() reconnects cleanly instead of reading a stale reply.
            // err-only (no {code} in result), same transport-error shape as a
            // connection failure, so callers toast it.
            err = "engine call timed out after " + std::to_string(timeoutMs) + "ms";
            closeConnection();
            return false;
        }
        const auto rawEvent = rawMember(line, "event");
        if (!rawEvent.empty() && (!onEvent || rawEvent != "\"agent.event\"")) {
            if (!discardEvents_) retainEventLine(std::move(line));
            continue;
        }
        json msg = json::parse(line, nullptr, /*allow_exceptions=*/false);
        if (msg.is_discarded()) continue;  // tolerate stray non-JSON lines
        if (msg.contains("event")) {
            // Live agent-event streaming: fire onEvent NOW (mid-call) for agent.event so a
            // blocking agent run streams instead of buffering until it returns. Other events
            // (doc.*) still queue for the GUI-thread drainEvents() — or are dropped on a
            // discarding lane that nothing drains.
            //
            // The callback receives the event PAYLOAD (`data`), never the {"event","data"}
            // envelope — the same shape WorkerClient's local-pipe path delivers, so a
            // consumer keying on data["kind"]/data["run_id"] works over BOTH transports.
            // Handing over the envelope instead cost the whole online chat lane its live
            // rendering: every mapped kind read as "" and no bubble, tool row or delta ever
            // appeared until the batched result landed minutes later.
            if (onEvent && msg.value("event", std::string()) == "agent.event") {
                const auto it = msg.find("data");
                try {
                    onEvent(it != msg.end() && it->is_object() ? *it : json::object());
                } catch (...) {}
            } else {
                retainEvent(std::move(msg));
            }
            continue;
        }
        const auto responseId = msg.find("id");
        if (responseId == msg.end() || !responseId->is_number_integer()) {
            // JSON-RPC parse/invalid-request errors have id:null because the peer
            // cannot correlate the rejected request. There is only one waiter on
            // this socket: surface that error, then retire the uncorrelated stream.
            // value("id", -1) throws on null (and aborts exception-disabled WASM).
            const auto error = msg.find("error");
            if (responseId != msg.end() && responseId->is_null() &&
                error != msg.end() && error->is_object() && !msg.contains("result")) {
                out = std::move(msg);
                closeConnection();
                return true;
            }
            err = "daemon response has an invalid request id";
            closeConnection();
            return false;
        }
        if (*responseId == id) {
            out = std::move(msg);
            return true;
        }
        // A response for another id can't happen with one-outstanding-request;
        // drop it rather than deadlock if the daemon misbehaves.
    }
}

// Reconnect after a timeout/EOF invalidated the connection (mtx_ held).
// Connect-only, never spawns: a timed-out daemon is usually alive-but-busy, and
// hello is a daemon-level method that answers even mid-solve. C++ equivalent of
// daemon_bridge.py DaemonClient.call's reconnect-on-EOF.
bool EngineClient::reconnectLocked(std::string& err, const std::atomic<bool>* cancelled) {
    if (cancelled && cancelled->load(std::memory_order_acquire)) {
        err = "aborted";
        return false;
    }
    // Publish this attempt's hello error (null unless the daemon refused) when it ends.
    json refusal;
    const auto publish = onExit([&] { setHelloError(std::move(refusal)); });
    if (sockPath_.empty()) {  // connectOrSpawn never ran (or never succeeded)
        err = "not connected to hostd";
        return false;
    }
    closeConnection();
    std::string cerr;
    if (!tryConnect(cerr, cancelled)) {
        err = "not connected to hostd (reconnect failed: " + cerr + ")";
        return false;
    }
    if (cancelled && cancelled->load(std::memory_order_acquire)) {
        closeConnection();
        err = "aborted";
        return false;
    }
    // Inline hello — hello() goes through call(), which would re-lock mtx_.
    // Bounded: a reconnect must never reintroduce an unbounded wait.
    const int id = nextId_++;
    json hparams = {{"client", clientKind_}, {"pid", selfPid()}, {"proto", 1}};
    if (!userId_.empty())
        hparams["user"] = {{"id", userId_}, {"display", userDisplay_}};
    json req = {{"id", id},
                {"method", "hello"},
                {"params", hparams}};
    if (!conn_.writeLine(req.dump())) {
        closeConnection();
        err = "daemon write failed (reconnect hello)";
        return false;
    }
    json resp;
    if (!readResponse(id, resp, err, /*timeoutMs=*/5000, {}, cancelled)) return false;
    if (!resp.contains("result")) {
        refusal = resp.value("error", json());
        if (refusal.is_object()) {
            refuseHello(refusal, err);
        } else {
            refusal = json();
            err = "hello failed on reconnect";
            closeConnection();
        }
        return false;
    }
    serverInfo_ = resp["result"];
    // Signal subscribers (GUI event bootstrap) that per-connection daemon state
    // — event subscriptions above all — was just reset by this new connection.
    reconnectGen_.fetch_add(1, std::memory_order_relaxed);
    return true;
}

bool EngineClient::tryReconnect(std::string& err, const std::atomic<bool>* cancelled) {
    std::unique_lock<std::mutex> lock(mtx_, std::defer_lock);
    if (cancelled) {
        while (!lock.try_lock()) {
            if (!connectDelay(10, cancelled)) { err = "aborted"; return false; }
        }
        if (connectCancelled(cancelled)) { err = "aborted"; return false; }
    } else {
        lock.lock();
    }
    if (conn_.valid() && !conn_.eof()) return true;
    return reconnectLocked(err, cancelled);
}

// Non-blocking twin of tryReconnect for the wasm render thread — see the header.
// try_to_lock: a failed acquire means a call() is mid-flight; return false so the
// frame loop skips this tick (keeping the browser event loop alive) instead of
// hard-blocking, which would deadlock against the WS onclose that must run on this
// same thread. Bootstraps sockPath_ so it also serves the very first connect.
bool EngineClient::tryReconnectNonBlocking(std::string& err, bool* deferred) {
    std::unique_lock<std::mutex> lk(mtx_, std::try_to_lock);
    if (!lk.owns_lock()) {
        err = "reconnect deferred (engine call in flight)";
        if (deferred) *deferred = true;
        return false;
    }
    if (conn_.valid() && !conn_.eof()) return true;
    if (sockPath_.empty()) {
        // First use: establish the instance key + socket path reconnectLocked needs.
        // The wasm transport ignores the path (it dials the gateway WS) but
        // reconnectLocked guards on a non-empty sockPath_. clientKind_ is "gui";
        // the gateway asserts the real per-user identity into the hello anyway.
        clientKind_ = "gui";
        primaryThread_ = std::this_thread::get_id();
        sockPath_ = socketPath(instanceKey(std::string()));
        if (sockPath_.empty()) sockPath_ = "ws://engine";  // sentinel; path unused in wasm
    }
    return reconnectLocked(err);
}

bool EngineClient::pollAlive() {
    // try_lock: same rule as drainEvents — never block the GUI behind a long RPC.
    std::unique_lock<std::mutex> lk(mtx_, std::try_to_lock);
    if (!lk.owns_lock()) return connected();
    if (!conn_.valid() || conn_.eof()) {
        connected_.store(false, std::memory_order_release);
        return false;
    }
    // Queue raw pushes; private lanes discard bytes without building JSON trees.
    pollEventsLocked();
    const bool alive = conn_.valid() && !conn_.eof();
    connected_.store(alive, std::memory_order_release);
    return alive;
}

bool EngineClient::call(const std::string& method, const json& params, json& result,
                        std::string& err, int timeoutMs,
                        const std::function<void(const json&)>& onEvent,
                        const std::atomic<bool>* cancelled) {
    if (cancelled && cancelled->load(std::memory_order_acquire)) {
        err = "aborted";
        return false;
    }
    if (usesBackgroundConnection(method, params))
        return backgroundClient()->call(method, params, result, err, timeoutMs, onEvent,
                                         cancelled);
#ifdef __EMSCRIPTEN__
    // Browser MAIN thread: a hard mutex wait would busy-block the event loop, so the
    // AsyncRpc-worker call currently holding mtx_ can never receive its WS onmessage to
    // finish and release — permanent deadlock (the "drag the arrow → page freezes/crashes"
    // class: an interactive main-thread sync call — checkpoint/push_pull probe/revert —
    // contends mtx_ with an in-flight preview). Spin with emscripten_sleep so the event
    // loop keeps pumping; the worker drains and releases, then we acquire. Worker/other
    // threads (which do NOT own the event loop) still hard-block normally.
    std::unique_lock<std::mutex> lk(mtx_, std::defer_lock);
    if (emscripten_is_main_runtime_thread()) {
        while (!lk.try_lock()) emscripten_sleep(1);
    } else {
        lk.lock();
    }
#else
    std::lock_guard<std::mutex> lk(mtx_);
#endif
    return callLocked(method, params, result, err, timeoutMs, onEvent, cancelled);
}

bool EngineClient::tryCall(const std::string& method, const json& params, json& result,
                           std::string& err, int timeoutMs, bool& deferred) {
    result = json();
    err.clear();
    std::unique_lock<std::mutex> lk(mtx_, std::try_to_lock);
    deferred = !lk.owns_lock();
    if (deferred) return false;
    // The frame loop owns reconnect/backoff; a tryCall must not reconnect inline.
    if (!conn_.valid() || conn_.eof()) {
        err = "not connected to hostd";
        return false;
    }
    return callLocked(method, params, result, err, timeoutMs);
}

bool EngineClient::callLocked(const std::string& method, const json& params, json& result,
                              std::string& err, int timeoutMs,
                              const std::function<void(const json&)>& onEvent,
                              const std::atomic<bool>* cancelled) {
    if (cancelled && cancelled->load(std::memory_order_acquire)) {
        err = "aborted";
        return false;
    }
    if ((!conn_.valid() || conn_.eof()) && !reconnectLocked(err, cancelled)) return false;
    if (cancelled && cancelled->load(std::memory_order_acquire)) {
        closeConnection();
        err = "aborted";
        return false;
    }
    const int id = nextId_++;
    json req = {{"id", id}, {"method", method}, {"params", params}};
    if (!conn_.writeLine(req.dump())) {
        closeConnection();
        err = "daemon write failed";
        return false;
    }
    json resp;
    if (!readResponse(id, resp, err, timeoutMs, onEvent, cancelled)) return false;
    if (resp.contains("result")) {
        result = resp["result"];
        return true;
    }
    result = resp.value("error", json::object());
    err = result.is_object() ? result.value("message", result.dump()) : result.dump();
    return false;
}

std::vector<json> EngineClient::drainEvents(double budgetMs) {
    pollBackgroundClients();
    std::unique_lock<std::mutex> lk(mtx_, std::try_to_lock);
    if (!lk.owns_lock()) return {};
    const auto deadline = std::chrono::steady_clock::now() +
        std::chrono::duration<double, std::milli>(budgetMs);
    pollEventsLocked();
    if (!conn_.valid() || conn_.eof())
        connected_.store(false, std::memory_order_release);
    std::vector<json> out;
    while (!events_.empty() &&
           (clientKind_ != "gui" || std::chrono::steady_clock::now() < deadline)) {
        auto& event = events_.front();
        json msg = event.line.empty() ? std::move(event.parsed)
                                    : json::parse(event.line, nullptr, false);
        events_.pop_front();
        if (!msg.is_discarded() && msg.contains("event")) out.push_back(std::move(msg));
    }
    pendingEvents_.store(!events_.empty() || pollLimited_, std::memory_order_release);
    return out;
}

}  // namespace chudware_ipc
