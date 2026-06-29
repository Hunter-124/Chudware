// Spawns the Python kernel worker (engine.worker) and talks to it over stdio with
// newline-delimited JSON-RPC. Windows pipes; one synchronous request/response per call.
#pragma once
#include <atomic>
#include <functional>
#include <mutex>
#include <string>

#include <nlohmann/json.hpp>

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

    // Resolve python exe + repo/cwd from env (AI_CAD_PYTHON, AI_CAD_REPO) or the
    // %APPDATA%/AI-CAD defaults, then spawn `python -m <module>`. The default module is the
    // doc worker; C19 spawns a second instance with module="engine.sim.worker".
    bool start(std::string& err, const std::string& module = "engine.worker");
    bool started() const { return running_; }
    void stop();

    // Send {method, params}; block until the matching response. Returns the "result"
    // object on success; fills err and returns false on transport/rpc error. While
    // waiting, any matching "event" notification lines are delivered to onEvent (if set).
    bool call(const std::string& method, const nlohmann::json& params,
              nlohmann::json& result, std::string& err, int timeoutMs = 120000,
              const EventFn& onEvent = {});

    // C39: one-way teardown abort — makes a blocked (and any future) call() fail fast with
    // err "aborted" instead of running out its timeout. Set at app exit (AsyncRpc::shutdown)
    // so a long in-flight RPC can't hang the close. The errorSink is NOT fired for aborts.
    void abortPending() { abort_.store(true); }

private:
    bool readLine(std::string& line, int timeoutMs);

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
    std::mutex callMtx_;             // serialize concurrent call()s
    ErrorSink errorSink_;            // W1-B: transport-error notification (optional)
    std::atomic<bool> abort_{false}; // C39: teardown abort (one-way; see abortPending)
};
