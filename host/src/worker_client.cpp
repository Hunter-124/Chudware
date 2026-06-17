#include "worker_client.h"

#include <windows.h>

#include <cstdlib>
#include <string>

#include "bootstrap.h"
#include "core/cpp_core.h"  // P16: route C++-backed documents in-process to libaicad

using nlohmann::json;

namespace {

// --- P16: per-document cutover routing to the pure-C++ core (libaicad) -------
// Opt-in via env AI_CAD_CPP_DOCS (non-empty, not "0"). A document CREATED in the C++
// core gets a "doc-cpp..." id, so every subsequent doc_* call carrying that id routes
// to the C++ core too — the id prefix IS the commit-at-doc_new marker, giving an
// all-or-nothing-per-document cutover with no separate routing state. Flag OFF (default)
// is byte-for-byte today's behavior: every call goes to the Python worker.
// The C++ core is now the DEFAULT engine for new PART documents. Set AI_CAD_CPP_DOCS=0
// to force the legacy Python worker for everything (the pre-cutover behavior).
bool cppDocsFlag() {
    const char* f = std::getenv("AI_CAD_CPP_DOCS");
    if (f && f[0]) return f[0] != '0';  // explicit override (1=on, 0=off)
    return true;                        // default: route new parts to the C++ core
}

// Methods that MINT a new document (vs. operate on an existing one). For these the C++
// core has not created a session yet when it reports "unsupported", so the host can
// safely fall back to the Python worker (no mid-document engine switch).
bool isMintingMethod(const std::string& method, const json& params) {
    if (method == "doc_new" || method == "doc_open") return true;
    if (method == "import_file") {
        auto into = params.is_object() ? params.find("into") : params.end();
        const bool hasInto = params.is_object() && into != params.end() && into->is_string() &&
                             !into->get<std::string>().empty();
        return !hasInto;  // import into a NEW doc mints; import into an existing doc does not
    }
    return false;
}
bool routeDocToCpp(const std::string& method, const json& params) {
    if (method == "doc_compile_cpp" || method == "doc_validate") return false;  // diagnostics
    // A C++-backed doc carries a doc-cpp id -> EVERY call operating on it routes to the
    // C++ core for that doc's whole lifetime (even if the flag was since toggled off).
    // This is NOT limited to doc_* — config_* / weld_* / export_* / etc. all carry the
    // doc_id and belong to the same document. An unported method then fails loud in the
    // core (no Python doc to fall back to), surfacing the gap rather than misrouting.
    if (params.is_object()) {
        // doc_id (most doc_* / config_* / export_*) or `into` (import_file appends to a
        // doc) carrying a doc-cpp prefix binds the call to the C++-backed document.
        for (const char* key : {"doc_id", "into"}) {
            auto it = params.find(key);
            if (it != params.end() && it->is_string() &&
                it->get<std::string>().rfind("doc-cpp", 0) == 0)
                return true;
        }
    }
    // doc_new / doc_open / import_file (no `into`) MINT a fresh doc -> route to C++ when
    // the flag is on. The C++ core handles only PART documents, so a doc_new that names
    // an assembly stays on Python; a doc_open of a non-part container is detected by the
    // core (code "unsupported") and falls back below. import_file with `into` is handled
    // by the prefix check above (it appends to whichever engine owns that doc).
    if (!cppDocsFlag()) return false;
    if (method == "doc_new") {
        const std::string kind = params.is_object() ? params.value("kind", std::string("part"))
                                                     : std::string("part");
        return kind.empty() || kind == "part";
    }
    if (method == "doc_open") return true;  // core rejects non-parts -> host falls back
    if (isMintingMethod(method, params)) return true;  // import_file minting a new doc
    return false;
}
std::wstring widen(const std::string& s) {
    if (s.empty()) return L"";
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
    std::wstring w(n, 0);
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), w.data(), n);
    return w;
}
bool fileExists(const std::string& p) {
    DWORD a = GetFileAttributesA(p.c_str());
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}
std::string win32Error(DWORD code) {
    char* msg = nullptr;
    DWORD n = FormatMessageA(
        FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
        nullptr, code, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT), (char*)&msg, 0, nullptr);
    std::string s = (n && msg) ? std::string(msg, n) : ("error " + std::to_string(code));
    if (msg) LocalFree(msg);
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r' || s.back() == '.')) s.pop_back();
    return s;
}
}  // namespace

WorkerClient::~WorkerClient() { stop(); }

bool WorkerClient::start(std::string& err, const std::string& module) {
    // Resolve via the shared bootstrap logic so the worker, runtimeReady(), and the GUI
    // all agree on which python + app dir to use.
    std::string python = runtimePython();
    std::string repo = appDir();
    readBuf_.clear();  // fresh process => fresh stream (matters when restarting after a repair)

    // Fail with a clear, actionable message *before* CreateProcess so a missing runtime
    // never surfaces as a cryptic "CreateProcess failed".
    if (!fileExists(python)) {
        err = "Python runtime not installed.\nExpected: " + python +
              "\nUse AI-CAD \xe2\x96\xb8 Repair runtime to install it.";
        return false;
    }

    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;

    HANDLE outRd = nullptr, outWr = nullptr, inRd = nullptr, inWr = nullptr;
    if (!CreatePipe(&outRd, &outWr, &sa, 0) || !CreatePipe(&inRd, &inWr, &sa, 0)) {
        err = "CreatePipe failed";
        return false;
    }
    // The parent's ends must not be inherited by the child.
    SetHandleInformation(outRd, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(inWr, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = inRd;
    si.hStdOutput = outWr;
    si.hStdError = GetStdHandle(STD_ERROR_HANDLE);  // worker diagnostics -> host console

    std::wstring cmd = L"\"" + widen(python) + L"\" -m " + widen(module);
    std::wstring cwd = widen(repo);
    std::vector<wchar_t> cmdbuf(cmd.begin(), cmd.end());
    cmdbuf.push_back(0);

    PROCESS_INFORMATION pi{};
    BOOL ok = CreateProcessW(nullptr, cmdbuf.data(), nullptr, nullptr, TRUE, CREATE_NO_WINDOW,
                             nullptr, cwd.empty() ? nullptr : cwd.c_str(), &si, &pi);
    DWORD gle = GetLastError();
    CloseHandle(outWr);
    CloseHandle(inRd);
    if (!ok) {
        err = "Could not launch the Python runtime (" + win32Error(gle) + ").\n" + python +
              "\nUse AI-CAD \xe2\x96\xb8 Repair runtime to reinstall it.";
        CloseHandle(outRd);
        CloseHandle(inWr);
        return false;
    }
    CloseHandle(pi.hThread);
    process_ = pi.hProcess;
    childStdoutRd_ = outRd;
    childStdinWr_ = inWr;
    running_ = true;
    return true;
}

void WorkerClient::stop() {
    if (childStdinWr_) {
        DWORD wrote = 0;
        const char* bye = "{\"id\":0,\"method\":\"shutdown\"}\n";
        WriteFile(childStdinWr_, bye, (DWORD)strlen(bye), &wrote, nullptr);
        CloseHandle(childStdinWr_);
        childStdinWr_ = nullptr;
    }
    if (process_) {
        WaitForSingleObject(process_, 2000);
        TerminateProcess(process_, 0);
        CloseHandle(process_);
        process_ = nullptr;
    }
    if (childStdoutRd_) {
        CloseHandle(childStdoutRd_);
        childStdoutRd_ = nullptr;
    }
    running_ = false;
}

bool WorkerClient::readLine(std::string& line, int timeoutMs) {
    ULONGLONG start = GetTickCount64();
    for (;;) {
        if (abort_.load()) return false;  // C39: teardown — stop polling immediately
        size_t nl = readBuf_.find('\n');
        if (nl != std::string::npos) {
            line = readBuf_.substr(0, nl);
            readBuf_.erase(0, nl + 1);
            return true;
        }
        if ((int)(GetTickCount64() - start) > timeoutMs) return false;

        DWORD avail = 0;
        if (!PeekNamedPipe(childStdoutRd_, nullptr, 0, nullptr, &avail, nullptr)) return false;
        if (avail == 0) {
            if (WaitForSingleObject(process_, 0) == WAIT_OBJECT_0) {
                // process exited; drain whatever is buffered one last time
                if (!PeekNamedPipe(childStdoutRd_, nullptr, 0, nullptr, &avail, nullptr) ||
                    avail == 0)
                    return false;
            } else {
                // W1-B (item 11): tighter poll. For the first ~2ms after the request was written,
                // yield with Sleep(0) (no enforced minimum sleep) so a fast doc_* round-trip returns
                // with sub-millisecond latency; past that, fall back to Sleep(1) to avoid burning a
                // core while waiting on a slow call. Measurable win across the 77 RPC call sites with
                // no architectural change (still a synchronous busy-poll).
                ULONGLONG waited = GetTickCount64() - start;
                if (waited < 2) Sleep(0);
                else Sleep(1);
                continue;
            }
        }
        std::string chunk(avail, 0);
        DWORD got = 0;
        if (!ReadFile(childStdoutRd_, chunk.data(), avail, &got, nullptr) || got == 0)
            return false;
        readBuf_.append(chunk.data(), got);
    }
}

bool WorkerClient::call(const std::string& method, const json& params, json& result,
                        std::string& err, int timeoutMs, const EventFn& onEvent) {
    std::lock_guard<std::mutex> lk(callMtx_);
    if (abort_.load()) {  // C39: app is tearing down — intentional, no errorSink toast
        err = "aborted";
        return false;
    }
    // P16: route C++-backed documents to the in-process core (libaicad) instead of the
    // Python worker. All-or-nothing per document (NO mid-doc Python fallback — Python
    // never created this doc), so an unported method surfaces a clear error rather than
    // silently misrouting. The diagnostic parity probe in doc_mirror is unaffected.
    if (routeDocToCpp(method, params)) {
        const bool minting = isMintingMethod(method, params);
        std::string cerr;
        if (core::cpp_core().call(method, params, result, cerr))
            return true;  // handled (aicad_call rc==0); `result` holds the ops-response
        // Nonzero rc: a structured OpError carries {code,message,detail} in `result`.
        if (result.is_object() && result.contains("code")) {
            // A MINTING call the core declined as "unsupported" (e.g. doc_open of an
            // assembly container) created no C++ doc — fall back to the Python worker
            // instead of surfacing the error (no mid-document engine switch happens).
            if (!(minting && result.value("code", std::string()) == "unsupported")) {
                err = result.value("message", std::string("cpp-core error"));
                return true;  // structured error reported (round-trip OK)
            }
            // else: fall through to the Python worker path below
        } else if (!minting) {
            // Hard failure (DLL missing / a throw) on a call bound to an EXISTING C++
            // doc — there is no Python doc to fall back to, so fail loudly.
            err = "cpp-core (" + method + "): " + cerr;
            if (errorSink_) errorSink_(method, err);
            return false;
        }
        // Minting-call fallback: the core could not mint this doc (unsupported kind or
        // the core DLL is unavailable) — continue to the Python worker path below so
        // document creation never hard-breaks when C++ is the default engine.
    }
    if (!running_) {
        err = "worker not started";
        if (errorSink_) errorSink_(method, err);  // W1-B: surface transport failures
        return false;
    }
    int id = nextId_++;
    json req = {{"id", id}, {"method", method}, {"params", params}};
    std::string line = req.dump() + "\n";
    DWORD wrote = 0;
    if (!WriteFile(childStdinWr_, line.data(), (DWORD)line.size(), &wrote, nullptr)) {
        err = "write failed";
        if (errorSink_) errorSink_(method, err);  // W1-B
        return false;
    }
    for (;;) {
        std::string resp;
        if (!readLine(resp, timeoutMs)) {
            if (abort_.load()) {  // C39: teardown abort, not a worker failure
                err = "aborted";
                return false;
            }
            err = "timeout/closed waiting for response";
            if (errorSink_) errorSink_(method, err);  // W1-B
            return false;
        }
        if (resp.empty()) continue;
        json j;
        try {
            j = json::parse(resp);
        } catch (...) {
            continue;  // ignore non-JSON noise on the channel
        }
        if (!j.contains("id") || j["id"].is_null() || j["id"].get<int>() != id) continue;
        // Streamed progress notification (no "ok"): deliver and keep waiting.
        if (j.contains("event") && !j.contains("ok")) {
            if (onEvent) onEvent(j.value("data", json::object()));
            continue;
        }
        if (j.value("ok", false)) {
            result = j.value("result", json::object());
            // P-session: doc_list must enumerate BOTH engines during the mixed-cutover
            // phase. The Python worker only knows its own docs; append the C++ core's
            // open sessions. Gated on available() — the DLL only loads once a C++-backed
            // doc exists, so when the cutover flag is off this is byte-for-byte today's
            // behavior (no C++ docs, no merge).
            if (method == "doc_list" && core::cpp_core().available() &&
                result.is_object() && result.contains("docs") && result["docs"].is_array()) {
                json cppList;
                std::string cerr;
                if (core::cpp_core().call("doc_list", json::object(), cppList, cerr) &&
                    cppList.is_object() && cppList.contains("docs") && cppList["docs"].is_array()) {
                    for (auto& d : cppList["docs"]) result["docs"].push_back(d);
                }
            }
            return true;
        }
        // Protocol-2 methods report errors as a structured envelope {code,message,detail}.
        // Deliver it in `result` (doc_mirror checks result.contains("code")) and return true:
        // the RPC round-trip succeeded, the METHOD reported an error. Legacy methods use a
        // plain string error and keep the false-return contract. Transport failures stay false.
        {
            const auto it = j.find("error");
            if (it != j.end() && it->is_object()) {
                result = *it;
                err = it->value("message", std::string("rpc error"));
                return true;
            }
            err = (it != j.end() && it->is_string()) ? it->get<std::string>()
                                                     : std::string("unknown rpc error");
            return false;
        }
    }
}
