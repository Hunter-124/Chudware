// Spawns the services worker sidecar and talks to it over stdio with newline-delimited
// JSON-RPC. Windows pipes; one synchronous request/response per call.
#pragma once
#include <atomic>
#include <cstddef>
#include <functional>
#include <mutex>
#include <string>
#include <utility>

#include <nlohmann/json.hpp>

namespace chudware::worker::detail {

constexpr std::size_t kControlRequestLimit = 64 * 1024;
constexpr std::size_t kControlResponseLimit = 256 * 1024;
constexpr int kControlTimeoutMs = 2500;

enum class ControlTransport { None, Unix, NamedPipe };
struct ControlInfo {
    ControlTransport transport = ControlTransport::None;
    std::string endpoint;
    std::string token;
};

inline void secureClear(std::string& value) noexcept {
    volatile char* bytes = value.empty() ? nullptr : &value[0];
    for (std::size_t i = 0; i < value.size(); ++i)
        bytes[i] = 0;
    value.clear();
}

inline bool matchesActiveRun(const std::string& activeRunId,
                             const std::string& requestedRunId) {
    return !requestedRunId.empty() && activeRunId == requestedRunId;
}

inline bool parseControlInfo(nlohmann::json& value, bool windows,
                             ControlInfo& out, std::string& err) {
    if (!value.is_object()) {
        err = "worker control info must be an object";
        return false;
    }
    auto transportIt = value.find("transport");
    auto endpointIt = value.find("endpoint");
    auto tokenIt = value.find("token");
    if (transportIt == value.end() || !transportIt->is_string() ||
        endpointIt == value.end() || !endpointIt->is_string() ||
        tokenIt == value.end() || !tokenIt->is_string()) {
        err = "worker control info is incomplete";
        return false;
    }
    const std::string& transport = transportIt->get_ref<const std::string&>();
    const std::string& endpoint = endpointIt->get_ref<const std::string&>();
    std::string& token = tokenIt->get_ref<std::string&>();
    if (token.size() != 64) {
        err = "worker control token is invalid";
        return false;
    }
    for (char ch : token) {
        if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'))) {
            err = "worker control token is invalid";
            return false;
        }
    }
    for (unsigned char ch : endpoint) {
        if (ch < 0x20 || ch == 0x7f) {
            err = "worker control endpoint is invalid";
            return false;
        }
    }
    ControlInfo parsed;
    if (windows) {
        constexpr const char* prefix = "\\\\.\\pipe\\chudware-control-S-";
        const std::size_t prefixSize = std::char_traits<char>::length(prefix);
        const std::size_t separator = endpoint.rfind('-');
        if (transport != "named_pipe" || endpoint.rfind(prefix, 0) != 0 ||
            endpoint.size() > 240 || separator == std::string::npos ||
            separator <= prefixSize || endpoint.size() - separator - 1 != 64) {
            err = "worker control named pipe endpoint is invalid";
            return false;
        }
        for (std::size_t i = prefixSize; i < separator; ++i) {
            if ((endpoint[i] < '0' || endpoint[i] > '9') && endpoint[i] != '-') {
                err = "worker control pipe owner SID is invalid";
                return false;
            }
        }
        for (std::size_t i = separator + 1; i < endpoint.size(); ++i) {
            const char ch = endpoint[i];
            if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'))) {
                err = "worker control pipe nonce is invalid";
                return false;
            }
        }
        parsed.transport = ControlTransport::NamedPipe;
    } else {
        // 103 bytes fits the smallest sockaddr_un::sun_path supported by the
        // desktop targets, including its terminating NUL.
        if (transport != "unix" || endpoint.empty() || endpoint.front() != '/' ||
            endpoint.size() > 103) {
            err = "worker control Unix endpoint is invalid";
            return false;
        }
        parsed.transport = ControlTransport::Unix;
    }
    parsed.endpoint = endpoint;
    parsed.token = std::move(token);
    secureClear(out.token);
    out = std::move(parsed);
    return true;
}

inline std::string sanitizeControlError(std::string message,
                                        const std::string& token) {
    if (!token.empty()) {
        for (std::size_t pos = message.find(token); pos != std::string::npos;
             pos = message.find(token, pos + 10))
            message.replace(pos, token.size(), "<redacted>");
    }
    if (message.size() > 512)
        message.resize(512);
    for (char& ch : message) {
        const unsigned char byte = static_cast<unsigned char>(ch);
        if (byte < 0x20 || byte == 0x7f)
            ch = ' ';
    }
    return message;
}

inline bool buildControlFrame(const std::string& token,
                              const std::string& action,
                              const nlohmann::json& params,
                              std::string& frame, std::string& err) {
    if (action != "cancel" && action != "steer" && action != "answer" &&
        action != "status") {
        err = "unsupported worker control action";
        return false;
    }
    if (!params.is_object()) {
        err = "worker control params must be an object";
        return false;
    }
    std::string paramsText;
    try {
        paramsText = params.dump();
    } catch (...) {
        err = "worker control params are not serializable";
        return false;
    }
    frame.clear();
    frame.reserve(token.size() + action.size() + paramsText.size() + 40);
    frame = "{\"token\":\"";
    frame += token;
    frame += "\",\"action\":\"";
    frame += action;
    frame += "\",\"params\":";
    frame += paramsText;
    frame += "}\n";
    if (frame.size() > kControlRequestLimit) {
        secureClear(frame);
        err = "worker control request exceeded limit";
        return false;
    }
    return true;
}

inline bool parseControlResponse(const std::string& frame,
                                 const std::string& token,
                                 nlohmann::json& result, std::string& err) {
    if (frame.size() > kControlResponseLimit) {
        err = "worker control response exceeded limit";
        return false;
    }
    if (!token.empty() && frame.find(token) != std::string::npos) {
        err = "worker control response exposed authentication material";
        return false;
    }
    nlohmann::json response;
    try {
        response = nlohmann::json::parse(frame);
    } catch (...) {
        err = "worker control returned malformed JSON";
        return false;
    }
    if (!response.is_object()) {
        err = "worker control returned malformed response";
        return false;
    }
    auto okIt = response.find("ok");
    if (okIt == response.end() || !okIt->is_boolean()) {
        err = "worker control returned malformed response";
        return false;
    }
    if (okIt->get<bool>()) {
        auto it = response.find("result");
        if (it == response.end() || !it->is_object()) {
            err = "worker control returned malformed result";
            return false;
        }
        result = *it;
        return true;
    }
    auto errorIt = response.find("error");
    std::string message = "worker control request failed";
    if (errorIt != response.end() && errorIt->is_object()) {
        auto messageIt = errorIt->find("message");
        if (messageIt != errorIt->end() && messageIt->is_string())
            message = messageIt->get<std::string>();
    }
    err = sanitizeControlError(std::move(message), token);
    return false;
}

} // namespace chudware::worker::detail

class WorkerClient {
public:
    ~WorkerClient();

    // Called (on the calling thread) for each streamed progress notification that arrives
    // before the final response — receives the event's "data" object.
    using EventFn = std::function<void(const nlohmann::json&)>;

    // W1-B (item 11): optional error sink, invoked (on the calling thread) whenever a call() fails
    // at the transport layer — timeout / closed pipe / write failure / "worker not started". Lets
    // app.cpp route otherwise-silent RPC failures to a toast, even from call sites it doesn't own.
    // NOT fired for method-level errors that succeed at transport (those return via `result`).
    using ErrorSink = std::function<void(const std::string& method, const std::string& err)>;
    void setErrorSink(ErrorSink sink) { errorSink_ = std::move(sink); }

    // Packaged builds map only services.worker_main to the canonical adjacent
    // chudware-worker binary and launch it with `--module worker`; missing binaries,
    // non-adjacent inherited overrides, and unknown modules fail closed. Developer builds
    // retain CHUDWARE_WORKER_BIN / CHUDWARE_PYTHON source execution.
    bool start(std::string& err, const std::string& module = "services.worker_main");
    bool started() const { return running_; }
    // Synthetic ready flag for daemon/WASM (and in-process libchudware FEA): no child process,
    // but launch()/caps() gates and non-routed methods need started()==true. routeSimToCpp
    // still runs before the pipe write; markStarted does not open a pipe.
    void markStarted() { running_ = true; }
    void stop();

    // A joined PLM live document sends every ordinary doc_apply_ops batch through
    // the live proposal relay. These helpers expose only process-local route
    // membership; they do not contact the worker or mutate a document.
    bool liveDocJoined(const std::string& docId) const;
    void clearLiveDocRoute(const std::string& docId);
    void clearLiveDocRoutes();

    // Send {method, params}; block until the matching response. Returns the "result"
    // object on success; fills err and returns false on transport/rpc error. While
    // waiting, any matching "event" notification lines are delivered to onEvent (if set).
    // timeoutMs < 0 has no deadline (daemon socket and local pipe alike): the call ends on
    // its response, peer/child loss or teardown abort.
    bool call(const std::string& method, const nlohmann::json& params,
              nlohmann::json& result, std::string& err, int timeoutMs = 120000,
              const EventFn& onEvent = {});

    // Best-effort agent_cancel {run_id, provider?} → {ok, cancelled, carried_steer_ids?}.
    // Native controls bypass the call mutex / use a daemon side socket. Browser controls
    // use cell admission and the provider captured at launch, never the current picker.
    // carried_steer_ids: steers Stop withdrew before the agent read them.
    bool callAgentCancel(const std::string& runId, nlohmann::json& result, std::string& err,
                         const std::string& provider = {});

    // Mid-run steering: agent_steer {run_id, text, attachments?, steer_id?} → {ok, steered,
    // steer_id}. Same mutex-bypass / side-channel transport as cancel — a steer that queued
    // behind the in-flight run would only land after the run finished, i.e. never.
    // `attachments` is the composer's attachment array (images ride to Chudcode as image
    // parts); empty = text-only. `steerId` correlates the agent.steer delivery events.
    bool callAgentSteer(const std::string& runId, const std::string& text,
                        const nlohmann::json& attachments, const std::string& steerId,
                        nlohmann::json& result, std::string& err,
                        const std::string& provider = {});

    // Interactive ask_user answer: agent_answer {run_id, batch_id, answers} or
    // {run_id, batch_id, declined:true} → {ok, run_id, batch_id, answered} | {ok:false, reason}.
    // Same mutex-bypass / side-channel transport as steer: the question blocks the in-flight
    // run, so an answer queued behind it would deadlock. `answers` maps question id → raw
    // typed value (contracts/interaction.md); ignored when `declined`.
    bool callAgentAnswer(const std::string& runId, const std::string& batchId,
                         const nlohmann::json& answers, bool declined,
                         nlohmann::json& result, std::string& err,
                         const std::string& provider = {});

    // C39: one-way teardown abort — makes a blocked (and any future) call() fail fast with
    // err "aborted" instead of running out its timeout. Set at app exit (AsyncRpc::shutdown)
    // so a long in-flight RPC can't hang the close. The errorSink is NOT fired for aborts.
    // Also aborts the daemon EngineClient socket so agent_run_v2 mid-flight does not pin quit.
    void abortPending();

private:
    // Applies the process-local PLM live-route overlay around callImpl. Keeping
    // this outside callImpl lets a relayed doc_apply_ops enter the generic
    // plm_live_propose seam without recursively intercepting itself.
    bool callWithLiveRouting(const std::string& method,
                             const nlohmann::json& params,
                             nlohmann::json& result, std::string& err,
                             int timeoutMs, const EventFn& onEvent);

    // Real backend-routing body; the public call() wraps callWithLiveRouting
    // with the T-G9 parity trace (CHUDWARE_RPC_TRACE=<file>) so the trace still
    // records the caller-visible method, params, and unwrapped result.
    bool callImpl(const std::string& method, const nlohmann::json& params,
                  nlohmann::json& result, std::string& err, int timeoutMs,
                  const EventFn& onEvent);

    // Daemon-mode fallback when another call (e.g. a slow doc_rebuild) holds
    // callMtx_: run this call on a short-lived side EngineClient connection
    // (the same pattern callAgentCancel/callAgentSteer use) instead of freezing
    // the caller behind it. hostd serves each connection on its own thread,
    // and its doc registry gates multi-writer overlap, so a concurrent read or
    // edit is safe. Only used for engine-bound calls — pipe-bound methods still
    // serialize on callMtx_.
    bool callViaSideClient(const std::string& method, const nlohmann::json& params,
                           nlohmann::json& result, std::string& err, int timeoutMs,
                           const EventFn& onEvent);
    // Local-worker pipe body. callImpl owns callMtx_ before entering this path;
    // keeping the pipe exchange separate lets agent_admin_turn synchronously
    // obtain worker_control_info on the same serialized stream before its long
    // request starts.
    bool callWorkerPipe(const std::string& method, const nlohmann::json& params,
                        nlohmann::json& result, std::string& err, int timeoutMs,
                        const EventFn& onEvent);

    using WorkerControlInfo = chudware::worker::detail::ControlInfo;
    bool ensureWorkerControlInfo(std::string& err);
    bool installWorkerControlInfo(nlohmann::json& info, std::string& err);
    bool callWorkerControl(const std::string& action, const std::string& runId,
                           const nlohmann::json& params, bool& matched,
                           nlohmann::json& result, std::string& err);
    void clearWorkerControlInfo();
    void clearWorkerControlState();


    enum class ReadLineResult { Line, Timeout, Closed };
    ReadLineResult readLine(std::string& line, int timeoutMs);
    bool terminatePoisonedWorker(std::string& err);
    bool restartPoisonedWorker(std::string& err);

#ifdef _WIN32
    void* childStdinWr_ = nullptr;   // HANDLE
    void* childStdoutRd_ = nullptr;  // HANDLE
    void* process_ = nullptr;        // HANDLE
#else
    int childStdinWr_ = -1;          // write fd -> child stdin
    int childStdoutRd_ = -1;         // read fd  <- child stdout
    int process_ = -1;               // child pid
#endif
    std::string readBuf_;
    int nextId_ = 1;
    bool running_ = false;
    std::string workerModule_ = "services.worker_main";
    bool poisoned_ = false;
    // Local stdio and primary document calls serialize here. Allowlisted GUI
    // background queries bypass it and use EngineClient's private per-thread sockets.
    std::mutex callMtx_;
    // Native daemon agent runs serialize here, off callMtx_, so worker-routed
    // GUI requests stay responsive. Online daemon turns take neither mutex:
    // hostd gives each its own worker and engine_backend leases a connection.
    // Pipe mode keeps callMtx_ for its single local worker.
    std::mutex longRunMtx_;
    ErrorSink errorSink_;            // W1-B: transport-error notification (optional)
    std::atomic<bool> abort_{false}; // C39: teardown abort (one-way; see abortPending)
    // Independent from callMtx_: cancel/steer must snapshot the authenticated
    // endpoint while the long stdio request owns callMtx_ and reads stdout.
    std::mutex workerControlMtx_;
    WorkerControlInfo workerControl_;
    std::string activeWorkerRunId_;
};
