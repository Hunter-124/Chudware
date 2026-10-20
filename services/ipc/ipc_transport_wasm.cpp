// EMSCRIPTEN (browser) implementation of the hostd IPC transport.
//
// Newline-delimited JSON-RPC over a WebSocket to the gateway `WS /engine`.
// The unix-path argument to connectTo() is ignored; the client dials
//   ws(s)://location.host/engine
// (ws:// when location.protocol is "http:", else wss://).
//
// Link requirement (integrator): -lwebsocket.js
//
// Current model (main-thread ImGui + ASYNCIFY): WS callbacks run on the main
// runtime thread; connect/readLine yield with emscripten_sleep (NOT condvar park)
// so the browser event loop can deliver onmessage while a call waits. Requires
// -sASYNCIFY on the final link. A prior PROXY_TO_PTHREAD/condvar design was abandoned.
//
// Server-side IpcListener is stubbed (browser is always the client). spawnDetached
// lives in host/src/wasm_stubs.cpp (and ipc_discovery on native) — NOT redefined
// here to avoid duplicate symbols.
//
// Native builds never compile this file (self-guard + CMake EMSCRIPTEN branch).
#ifdef __EMSCRIPTEN__

#include "ipc_transport.h"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <pthread.h>
#include <string>

#include <emscripten.h>
#include <emscripten/websocket.h>

namespace chudware_ipc {

// Heap-shared connection state. Callbacks hold a raw pointer to this; IpcConn
// holds a shared_ptr so the state outlives any moved-from IpcConn shell.
struct IpcConn::WasmState {
    EMSCRIPTEN_WEBSOCKET_T sock = 0;  // 0 = closed / never opened
    std::mutex mu;
    std::condition_variable cv;
    std::string buf;                  // incomplete + complete lines, split on '\n'
    bool open = false;                // true once onopen fires
    bool closed = false;              // true once onclose/onerror/close()
    bool connectFailed = false;       // open never completed (error before open)
    std::atomic<bool> eof{false};     // peer dead / write failed — mirrors native eof_
};

namespace {

// location.host + protocol → "ws://host/engine" or "wss://host/engine".
// EM_ASM_INT returns a malloc'd C string we free.
std::string gatewayWsUrl() {
    char* raw = (char*)EM_ASM_PTR({
        var proto = (typeof location !== 'undefined' && location.protocol === 'http:')
                        ? 'ws:'
                        : 'wss:';
        var host = (typeof location !== 'undefined' && location.host)
                       ? location.host
                       : 'localhost';
        var url = proto + '//' + host + '/engine';
        var len = lengthBytesUTF8(url) + 1;
        var ptr = _malloc(len);
        stringToUTF8(url, ptr, len);
        return ptr;
    });
    std::string url = raw ? raw : "wss://localhost/engine";
    free(raw);
    return url;
}

bool onOpen(int /*eventType*/, const EmscriptenWebSocketOpenEvent* /*e*/, void* userData) {
    auto* st = static_cast<IpcConn::WasmState*>(userData);
    {
        std::lock_guard<std::mutex> lk(st->mu);
        st->open = true;
    }
    st->cv.notify_all();
    return true;
}

bool onMessage(int /*eventType*/, const EmscriptenWebSocketMessageEvent* e, void* userData) {
    auto* st = static_cast<IpcConn::WasmState*>(userData);
    if (!e || !e->data || e->numBytes == 0) return true;
    {
        std::lock_guard<std::mutex> lk(st->mu);
        // emscripten text frames: numBytes = utf8_len+1 (includes trailing NUL).
        // Binary frames: raw byte count. Strip the NUL so JSON lines stay clean.
        size_t n = (size_t)e->numBytes;
        if (e->isText && n > 0 && e->data[n - 1] == 0) --n;
        if (n == 0) return true;
        // Gateway sends one JSON object per WS text frame WITH the trailing '\n'
        // stripped (engine_bridge daemon_to_ws rstrip). Native unix IPC is
        // newline-delimited; restore the delimiter so readLine() can split.
        st->buf.append(reinterpret_cast<const char*>(e->data), n);
        if (st->buf.empty() || st->buf.back() != '\n') st->buf.push_back('\n');
    }
    st->cv.notify_all();
    return true;
}

bool onError(int /*eventType*/, const EmscriptenWebSocketErrorEvent* /*e*/, void* userData) {
    auto* st = static_cast<IpcConn::WasmState*>(userData);
    {
        std::lock_guard<std::mutex> lk(st->mu);
        if (!st->open) st->connectFailed = true;
        st->closed = true;
        st->eof.store(true, std::memory_order_release);
    }
    st->cv.notify_all();
    return true;
}

bool onClose(int /*eventType*/, const EmscriptenWebSocketCloseEvent* /*e*/, void* userData) {
    auto* st = static_cast<IpcConn::WasmState*>(userData);
    {
        std::lock_guard<std::mutex> lk(st->mu);
        st->closed = true;
        st->eof.store(true, std::memory_order_release);
    }
    st->cv.notify_all();
    return true;
}

// Tear down the JS WebSocket handle. Safe to call more than once.
void destroySocket(IpcConn::WasmState& st) {
    EMSCRIPTEN_WEBSOCKET_T s = 0;
    {
        std::lock_guard<std::mutex> lk(st.mu);
        s = st.sock;
        st.sock = 0;
        st.closed = true;
        st.eof.store(true, std::memory_order_release);
    }
    if (s > 0) {
        // Best-effort close then free the handle. Order matches emscripten docs:
        // close() drains outbound; delete() drops the handle.
        emscripten_websocket_close(s, 1000, "client close");
        emscripten_websocket_delete(s);
    }
    st.cv.notify_all();
}

}  // namespace

// ------------------------------------------------------------------ IpcConn

IpcConn::IpcConn(std::shared_ptr<WasmState> ws) : ws_(std::move(ws)) {}

IpcConn& IpcConn::operator=(IpcConn&& o) noexcept {
    if (this != &o) {
        close();
        ws_ = std::move(o.ws_);
        o.ws_.reset();
    }
    return *this;
}

bool IpcConn::valid() const {
    // Native: fd_ >= 0 until close() zeroes it. Peer death sets eof_ but leaves
    // the fd open. Mirror that: sock is zeroed only by our close()/destroySocket;
    // onclose/onerror set eof without clearing sock.
    return ws_ && ws_->sock > 0;
}

bool IpcConn::eof() const {
    // Default/moved-from: no peer was ever attached — not an EOF signal (native
    // default-constructs eof_=false with fd_=-1). Only a live WasmState can
    // report peer death.
    if (!ws_) return false;
    return ws_->eof.load(std::memory_order_acquire);
}

void IpcConn::close() {
    if (!ws_) return;
    destroySocket(*ws_);
    // Keep the shared_ptr so a late main-thread callback still has a live
    // WasmState (sock already 0). valid() is false because sock is cleared.
    // The shared_ptr drops when this IpcConn is destroyed or moved-from.
}

void IpcConn::shutdown() {
    // Browser client is never the daemon side (no ~Server in wasm), but define the
    // symbol for ABI parity: tearing down the socket wakes any pending readLine.
    if (ws_) destroySocket(*ws_);
}

bool IpcConn::readLine(std::string& line, int timeoutMs) {
    if (!ws_) return false;
    auto& st = *ws_;

    // ASYNCIFY: never park on condvar — that freezes the main-thread mailbox so
    // onmessage (MAIN_RUNTIME_THREAD) cannot run. Poll + emscripten_sleep instead.
    const auto start = std::chrono::steady_clock::now();
    for (;;) {
        {
            std::lock_guard<std::mutex> lk(st.mu);
            const size_t nl = st.buf.find('\n');
            if (nl != std::string::npos) {
                line = st.buf.substr(0, nl);
                st.buf.erase(0, nl + 1);
                return true;
            }
            if (st.eof.load(std::memory_order_relaxed) || st.closed) return false;
        }
        if (timeoutMs == 0) return false;
        if (timeoutMs > 0) {
            const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                                     std::chrono::steady_clock::now() - start)
                                     .count();
            if ((int)elapsed >= timeoutMs) return false;
        }
        emscripten_sleep(timeoutMs == 0 ? 0 : 5);
    }
}

bool IpcConn::writeLine(const std::string& line) {
    if (!ws_) return false;
    auto& st = *ws_;

    EMSCRIPTEN_WEBSOCKET_T sock = 0;
    {
        std::lock_guard<std::mutex> lk(st.mu);
        if (st.sock <= 0 || st.closed || !st.open) {
            st.eof.store(true, std::memory_order_release);
            return false;
        }
        sock = st.sock;
    }

    // Text frame of `line` + '\n' (gateway expects newline-delimited JSON-RPC).
    std::string out = line;
    out.push_back('\n');
    const EMSCRIPTEN_RESULT rc =
        emscripten_websocket_send_utf8_text(sock, out.c_str());
    if (rc != EMSCRIPTEN_RESULT_SUCCESS) {
        st.eof.store(true, std::memory_order_release);
        {
            std::lock_guard<std::mutex> lk(st.mu);
            st.closed = true;
        }
        st.cv.notify_all();
        return false;
    }
    return true;
}

// --------------------------------------------------------------- IpcListener
// Browser client never listens. Stub the daemon-side surface so the header's
// declarations resolve without dragging AF_UNIX into the wasm link.

IpcListener& IpcListener::operator=(IpcListener&& o) noexcept {
    if (this != &o) {
        path_ = std::move(o.path_);
        o.path_.clear();
    }
    return *this;
}

bool IpcListener::adoptListeningFd(int /*fd*/, std::string& err) {
    err = "WASM: inherited listeners are unsupported";
    return false;
}

bool IpcListener::listen(const std::string& /*path*/, std::string& err) {
    err = "WASM: IpcListener not available (browser is the WS client)";
    return false;
}

IpcConn IpcListener::accept(int /*timeoutMs*/) {
    return IpcConn();
}

std::string IpcListener::releasePath() {
    std::string p = std::move(path_);
    path_.clear();
    return p;
}

void IpcListener::close(bool /*unlink_path*/) {
    path_.clear();
}

// ----------------------------------------------------------------- connectTo

IpcConn connectTo(const std::string& /*path*/, std::string& err) {
    if (!emscripten_websocket_is_supported()) {
        err = "WebSocket not supported in this browser";
        return IpcConn();
    }

    const std::string url = gatewayWsUrl();

    auto st = std::make_shared<IpcConn::WasmState>();

    EmscriptenWebSocketCreateAttributes attr;
    emscripten_websocket_init_create_attributes(&attr);
    attr.url = url.c_str();
    attr.protocols = nullptr;
    // Main-thread ImGui + ASYNCIFY: socket + callbacks live on the browser main
    // thread. connectTo/readLine MUST yield via emscripten_sleep (not condvar wait)
    // so onopen/onmessage can run while we block for a line.
    attr.createOnMainThread = true;

    EMSCRIPTEN_WEBSOCKET_T sock = emscripten_websocket_new(&attr);
    if (sock <= 0) {
        err = "emscripten_websocket_new failed for " + url;
        return IpcConn();
    }
    st->sock = sock;

    const pthread_t mainRt = EM_CALLBACK_THREAD_CONTEXT_MAIN_RUNTIME_THREAD;
    emscripten_websocket_set_onopen_callback_on_thread(sock, st.get(), onOpen, mainRt);
    emscripten_websocket_set_onmessage_callback_on_thread(sock, st.get(), onMessage, mainRt);
    emscripten_websocket_set_onerror_callback_on_thread(sock, st.get(), onError, mainRt);
    emscripten_websocket_set_onclose_callback_on_thread(sock, st.get(), onClose, mainRt);

    // Block until open or hard failure. connectTo is synchronous on native
    // (connect() returns only after the handshake); mirror that here so
    // callers can writeLine immediately on success.
    {
        // ASYNCIFY: do not block on condvar — that freezes this pthread's mailbox so
        // onopen/onmessage (pinned here) never run. Sleep yields and pumps callbacks.
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(15);
        for (;;) {
            {
                std::lock_guard<std::mutex> lk(st->mu);
                if (st->open) break;
                if (st->closed || st->connectFailed) break;
            }
            if (std::chrono::steady_clock::now() >= deadline) break;
            emscripten_sleep(10);
        }
        std::lock_guard<std::mutex> lk(st->mu);
        if (!st->open) {
            err = "connect " + url + ": websocket open timed out or failed";
            // unlock via guard end then destroy
        }
    }
    if (!st->open) {
        destroySocket(*st);
        if (err.empty()) err = "connect " + url + ": websocket open timed out or failed";
        return IpcConn();
    }

    return IpcConn(std::move(st));
}

}  // namespace chudware_ipc

#endif  // __EMSCRIPTEN__
