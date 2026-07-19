#include "worker_client.h"

#if defined(_WIN32)
#include <windows.h>
#elif !defined(__EMSCRIPTEN__)
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cerrno>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <thread>
#include <vector>
#endif

#include <cstdio>      // T-G9 parity trace (AI_CAD_RPC_TRACE) file append
#include "../ipc/ipc_client.h"
#include <functional>  // T-G9 parity trace secret redaction
#include <cstdlib>
#include <string>

#include "bootstrap.h"
#include "cpp_core.h"        // P16: legacy in-process libaicad (InProcCore backend)
#include "engine_backend.h"  // Phase 4: InProcCore vs DaemonConn seam (AI_CAD_ENGINE_DAEMON)

using nlohmann::json;

namespace {

// In-process live event streaming (desktop InProcCore path): bridge cpp_core's thread-local C
// event sink to the current call's EventFn so agent-run events reach the Copilot/timeline AS THEY
// HAPPEN instead of a batched replay after the (multi-minute) run. Daemon mode streams via hostd's
// agent.event push (engine_events) instead, so this in-proc sink is used only when !daemonMode().
thread_local WorkerClient::EventFn t_inproc_event_sink;
void inprocEventThunk(const char* ev_json, void*) {
    if (!t_inproc_event_sink || !ev_json) return;
    try {
        t_inproc_event_sink(nlohmann::json::parse(ev_json));
    } catch (...) {
    }
}
// Run an in-process agent engine::call with live streaming to onEvent. `streamedLive` reports
// whether events fired live (so the caller skips the batched replay to avoid doubles). RAII-clears
// the sink even if engine::call throws.
bool callAgentStreaming(const std::string& coreMethod, const json& p, json& cppResult,
                        std::string& cerr, int timeoutMs, const WorkerClient::EventFn& onEvent,
                        bool& streamedLive) {
    if (engine::daemonMode()) {
        // Daemon: hostd pushes {"event":"agent.event","data":<ev>}; engine::call fires our router
        // LIVE (mid-call, on this worker thread) for each. `fired` tells us whether the daemon
        // actually streamed (a same-build daemon does) so an OLD daemon that only returns the
        // batched result["events"] still falls back to the caller's replay.
        bool fired = false;
        auto router = [&](const json& evt) {
            if (onEvent && evt.is_object() && evt.contains("data") && evt["data"].is_object()) {
                fired = true;
                onEvent(evt["data"]);
            }
        };
        bool ok = engine::call(coreMethod, p, cppResult, cerr, timeoutMs, router);
        streamedLive = fired;
        return ok;
    }
    // In-process: the cpp_core thread-local sink fires each event live -> onEvent.
    streamedLive = static_cast<bool>(onEvent) && core::cpp_core().hasEventSink();
    struct Guard {
        bool on;
        ~Guard() {
            if (on) {
                core::cpp_core().setEventSink(nullptr, nullptr);
                t_inproc_event_sink = {};
            }
        }
    } guard{streamedLive};
    if (streamedLive) {
        t_inproc_event_sink = onEvent;
        core::cpp_core().setEventSink(&inprocEventThunk, nullptr);
    }
    return engine::call(coreMethod, p, cppResult, cerr, timeoutMs);
}

// --- P16: per-document cutover routing to the pure-C++ core (libaicad) -------
// Opt-in via env AI_CAD_CPP_DOCS (non-empty, not "0"). A document CREATED in the C++
// core gets a "doc-cpp..." id, so every subsequent doc_* call carrying that id routes
// to the C++ core too — the id prefix IS the commit-at-doc_new marker, giving an
// all-or-nothing-per-document cutover with no separate routing state. Flag OFF (default)
// is byte-for-byte today's behavior: every call goes to the Python worker.
//
// DEFAULT ON (2026-06-19): the C++ doc engine is parity-complete for parts / sketch mode /
// the full Evaluate suite / weldments / assemblies / copy-paste-as-derived, verified by the
// aggregate differential gate (tools/run_parity_suite.py — 24/24 vs the Python oracle: session
// 65, eval 40, sketch 22, weld 26, asm 10, derived 37, audit/audit2/audit3, + agent orchestration
// via the mock provider). Documents now default to the pure-C++ core. AI_CAD_CPP_DOCS=0 still
// forces the legacy Python worker (escape hatch / fallback while the Python oracle stays in-tree).
// The coupled cppAgentFlag() follows this, so the agent also defaults to C++ (it drives the same
// doc-cpp session it edits).
bool cppDocsFlag() {
    const char* f = std::getenv("AI_CAD_CPP_DOCS");
    if (f && f[0]) return f[0] != '0';  // explicit override (1=on, 0=force Python fallback)
    return true;                        // default: pure-C++ doc engine (parity-complete)
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
    // The parametric standard-parts catalog lives ONLY in the C++ core: parts_catalog_list
    // is doc-independent (family/size listing); parts_make mints a NEW doc-cpp part from the
    // generator. There is no Python equivalent, so always route to the core.
    if (method == "parts_catalog_list" || method == "parts_make") return true;
    // Agent RPCs are owned by the AGENT seam (routeAgentToCpp), which translates the host
    // names (agent_run_v2 -> agent_run, get_prompts_v2 -> agent_get_prompts_v2). They also
    // carry the active doc's doc_id, so without this guard the doc seam below would greedily
    // claim them by the doc-cpp prefix and call the C++ core with the UNtranslated name ->
    // "unknown method: agent_run_v2". Never route agent_* / get_prompts_v2 here.
    if (method.rfind("agent_", 0) == 0 || method == "get_prompts_v2") return false;
    // A C++-backed doc carries a doc-cpp id -> EVERY call operating on it routes to the
    // C++ core for that doc's whole lifetime (even if the flag was since toggled off).
    // This is NOT limited to doc_* — config_* / weld_* / export_* / etc. all carry the
    // doc_id and belong to the same document. An unported method then fails loud in the
    // core (no Python doc to fall back to), surfacing the gap rather than misrouting.
    if (params.is_object()) {
        // doc_id (most doc_* / config_* / export_*) or `into` (import_file appends to a
        // doc) binds the call to a C++-backed document. A part/assembly carries a "doc-cpp"
        // id; a 2D DRAWING session (drawing_new / drawing_add_* / drawing_get / drawing_bom /
        // export_drawing) carries a "dwg-cpp" id — both route to the C++ core.
        // eval_compare (WS-C reverse-engineering: params {doc_a, doc_b, ...}, no "doc_id") is
        // the one eval_* RPC that does NOT carry a plain "doc_id" -- without doc_a/doc_b here
        // it would silently miss this C++-only RPC and fall through to the (nonexistent, in
        // this RPC's case) Python doc path. eval_interference/eval_feature_overlap/
        // doc_face_wires all use "doc_id" and are already covered by the generic sniff below.
        for (const char* key : {"doc_id", "into", "doc_a", "doc_b"}) {
            auto it = params.find(key);
            if (it != params.end() && it->is_string()) {
                const std::string v = it->get<std::string>();
                if (v.rfind("doc-cpp", 0) == 0 || v.rfind("dwg-cpp", 0) == 0) return true;
            }
        }
        // drawing_new carries the SOURCE part by id (source_doc_id), not a doc_id of its
        // own yet — route it to C++ when the source part is C++-backed so the new drawing
        // is minted in (and stays bound to) the C++ core.
        auto sit = params.find("source_doc_id");
        if (sit != params.end() && sit->is_string() &&
            sit->get<std::string>().rfind("doc-cpp", 0) == 0)
            return true;
    }
    // doc_new / doc_open / import_file (no `into`) MINT a fresh doc -> route to C++ when
    // the flag is on. C++ now handles PART and ASSEMBLY documents (P57b compile + P57d
    // resolve_pick assembly branch + P57e primitive cut/merge — the asm scenario now seats
    // its peg at z=15, matching Python; cpp_asm_parity 10/10 incl. the host pick->mate->seat
    // path). A doc_open of a non-part/asm container is detected by the core (code
    // "unsupported") and falls back below.
    if (!cppDocsFlag()) return false;
    if (method == "doc_new") {
        const std::string kind = params.is_object() ? params.value("kind", std::string("part"))
                                                     : std::string("part");
        return kind.empty() || kind == "part" || kind == "assembly";
    }
    if (method == "doc_open") return true;  // core rejects non-parts -> host falls back
    if (isMintingMethod(method, params)) return true;  // import_file minting a new doc
    // vendor_part_insert mutates/mints a doc by applying an `import` feature; it now lives only in
    // the C++ core (the Python doc engine is gone). Route it there whenever it carries no doc-cpp
    // id yet (the prefix check above already catches the into/doc_id case).
    if (method == "vendor_part_insert") return true;
    return false;
}
// --- C++ AGENT routing (opt-in, default OFF) --------------------------------
// Route the in-app AI-agent RPCs (agent_run_v2 / agent_ask_v2 / agent_pm_assist_v1 /
// get_prompts_v2) to the pure-C++ core (libaicad) instead of the Python worker. This
// MIRRORS the doc_* seam but is DEFAULT OFF: unless AI_CAD_CPP_AGENT is set (and != "0")
// the app behaves byte-for-byte as today (every agent call goes to the Python worker).
// The legacy agent_design loop is NOT routed (stays Python). The C++ agent drives a
// C++-backed ("doc-cpp") doc session, so we only route when the active doc is C++-backed;
// a Python doc (e.g. an assembly) falls through to Python.
bool cppAgentFlag() {
    const char* f = std::getenv("AI_CAD_CPP_AGENT");
    if (f && f[0]) return f[0] != '0';  // explicit override (1=on, 0=force Python)
    // DEFAULT = FOLLOW THE DOC ENGINE. The agent drives the SAME doc session it edits, so it
    // MUST run on the same engine as the doc (C++ agent <-> doc-cpp; Python agent <-> Python
    // doc). Coupling the default to cppDocsFlag() makes the two impossible to mismatch: with
    // AI_CAD_CPP_DOCS=1 the agent also routes to C++ (so agent_run_v2 on a doc-cpp doc is
    // caught+translated by the agent seam, not mis-claimed by the doc seam); with docs on
    // Python (the current default) the agent stays Python. An explicit AI_CAD_CPP_AGENT still
    // overrides. (routeAgentToCpp additionally gates on the active doc actually being doc-cpp.)
    return cppDocsFlag();
}

// Is the active document a C++-backed ("doc-cpp") session? For agent_run_v2 /
// agent_ask_v2 the host passes the active doc_id directly in params; agent_pm_assist_v1
// carries it too. get_prompts_v2 has no doc and is always safe to route (read-only).
bool agentDocIsCpp(const std::string& method, const json& params) {
    if (method == "get_prompts_v2") return true;  // doc-independent (prompt catalog)
    // The pre-flight clarify gate fires on a Design-new submit BEFORE any part doc exists,
    // so it is doc-independent like get_prompts_v2: always safe to route to C++ (it only
    // reads the request + optional live-session context). This also loads the DLL lazily.
    if (method == "agent_clarify_v1") return true;
    // Cancel is process-wide registry keyed by run_id — no doc needed.
    if (method == "agent_cancel") return true;
    if (!params.is_object()) return false;
    auto it = params.find("doc_id");
    if (it == params.end() || !it->is_string()) return false;
    return it->get<std::string>().rfind("doc-cpp", 0) == 0;
}

bool routeAgentToCpp(const std::string& method, const json& params) {
    // Model-availability probe is doc-independent and provider-diagnostic; route it to the
    // C++ core whenever the DLL loads, regardless of the agent flag (the host picker falls
    // back to a static catalog if the core is unavailable).
    if (method == "agent_list_models") {
        std::string lerr;
        return engine::ensureLoaded(lerr);
    }
    if (!cppAgentFlag()) return false;
    if (!(method == "agent_run_v2" || method == "agent_ask_v2" ||
          method == "agent_clarify_v1" || method == "agent_pm_assist_v1" ||
          method == "agent_simulate_v1" || method == "agent_mission_v1" ||
          method == "agent_cancel" ||
          method == "get_prompts_v2"))
        return false;  // agent_design (legacy) + everything else stays Python
    if (!agentDocIsCpp(method, params)) return false;  // only C++-backed docs (or doc-indep)
    // Ensure the core DLL is loadable before committing to the C++ path. ensureLoaded is
    // idempotent (loads once); if it can't load, fall through to the Python worker so the
    // agent never hard-breaks. For a doc-cpp doc the DLL is already loaded (it minted the
    // doc); for get_prompts_v2 this triggers the lazy load on first use.
    std::string lerr;
    return engine::ensureLoaded(lerr);
}
// --- C++ SIM (FEA) routing (opt-in, default OFF) ----------------------------
// Route the host's FEA simulation RPCs (sim_mesh / sim_run_static / sim_run_modal /
// sim_run_fatigue / sim_run_thermal) to the pure-C++ core (libaicad) instead of the
// Python sim worker. MIRRORS the agent seam but is DEFAULT OFF: unless AI_CAD_CPP_SIM
// is set (and != "0") every sim call goes to the Python sim worker exactly as today.
// sim_run_flow (LBM/CFD) and sim_cancel are NEVER routed — they stay Python.
//
// The host's send-shape for sim_run_static/sim_run_modal is already the C++ contract
// ({study:{step_path, density, material, fixtures, loads}} + n_modes for modal), so no
// params translation is needed here. The C++ result carries summary + overlay_aicm,
// which is exactly what sim_panel consumes (it never reads results_npz/solve_time_s,
// the two fields the C++ result omits) — so the result is byte-compatible for display.
bool cppSimFlag() {
    const char* f = std::getenv("AI_CAD_CPP_SIM");
    if (f && f[0]) return f[0] != '0';  // explicit override (1=on, 0=force Python)
    return true;                        // default ON: FEA mesh/static/modal on the C++ core
}

bool routeSimToCpp(const std::string& method, const json& /*params*/) {
    if (!cppSimFlag()) return false;
    if (!(method == "sim_mesh" || method == "sim_run_static" || method == "sim_run_modal" ||
          method == "sim_run_fatigue" || method == "sim_run_thermal"))
        return false;  // sim_run_flow (LBM) + sim_cancel + everything else stays Python
    if (!engine::available()) {
        // Lazily try to load the core DLL; if it can't load, fall through to Python so
        // sim never hard-breaks. ensureLoaded is idempotent (loads once).
        std::string lerr;
        if (!engine::ensureLoaded(lerr)) return false;
    }
    return true;
}
// --- C++ ECAD routing (opt-in, default OFF) ---------------------------------
// Route the host's ECAD RPCs (ecad_*) to the pure-C++ core (libaicad) instead of
// the Python worker. MIRRORS the doc_* seam exactly, but is DEFAULT OFF: unless
// AI_CAD_CPP_ECAD is set (and != "0") every ecad_* call goes to the Python worker
// exactly as today. An ECAD doc CREATED in the C++ core mints an "ecadcpp..." id
// (core/ecad/session.cpp new_doc_id()), so every subsequent ecad_* call carrying
// that doc_id routes to the C++ core too — the id prefix IS the commit-at-mint
// marker, an all-or-nothing-per-document cutover (the ECAD analogue of "doc-cpp").
//
// Coverage gaps to be aware of when the flag is ON (these stay Python via the
// "unknown method" -> nonzero-without-{code} hard-fail or, for ecad_design, by
// minting a Python doc id): the C++ core handles the STATEFUL doc-id ecad methods
// in libaicad.cpp (ecad_new/open/apply_ops/undo/redo/tree/rebuild/close/describe/
// schema/dfm/impedance/export/save/update_from_sch/recipe*), but ecad_autoroute /
// ecad_to_3d / ecad_route_diff_pair are only ported for the STATELESS inline-board
// form, NOT the host's doc-id form; and ecad_fill_zones / ecad_simulate /
// ecad_design / ecad_import_kicad* are not in the C++ core at all (KiCad import
// stays Python). A call bound to an ecadcpp doc that hits one of these fails loud
// (no Python doc to fall back to) — exactly the doc-cpp seam's behavior.
bool cppEcadFlag() {
    const char* f = std::getenv("AI_CAD_CPP_ECAD");
    if (f && f[0]) return f[0] != '0';  // explicit override (1=on, 0=force Python)
#ifdef _WIN32
    return false;  // Windows: default to the mature Python ECAD worker
#else
    return true;   // Linux/macOS: no Python worker — the C++ core is the only ECAD backend
#endif
}

bool routeEcadToCpp(const std::string& method, const json& params) {
    if (method.rfind("ecad_", 0) != 0) return false;  // only ecad_* methods
    // A C++-backed ECAD doc carries an "ecadcpp" doc_id -> EVERY ecad_* call
    // operating on it routes to the C++ core for that doc's whole lifetime (even if
    // the flag was since toggled off). An unported method then fails loud in the
    // core (no Python doc to fall back to), surfacing the gap rather than misrouting.
    if (params.is_object()) {
        // doc_id (most ecad_*) or board_doc_id (ecad_update_from_sch) carrying an
        // ecadcpp prefix binds the call to the C++-backed ECAD document.
        for (const char* key : {"doc_id", "board_doc_id", "sch_doc_id"}) {
            auto it = params.find(key);
            if (it != params.end() && it->is_string() &&
                it->get<std::string>().rfind("ecadcpp", 0) == 0)
                return true;
        }
    }
    // ecad_new / ecad_open MINT a fresh ECAD doc -> route to C++ when the flag is on
    // and the core DLL is loadable. The core mints an "ecadcpp" doc; if it declines
    // (structured "unsupported") or the DLL is unavailable, the host falls back to
    // the Python worker so document creation never hard-breaks.
    if (!cppEcadFlag()) return false;
    if (method != "ecad_new" && method != "ecad_open") return false;  // minting only
    if (!engine::available()) {
        std::string lerr;
        if (!engine::ensureLoaded(lerr)) return false;  // can't load -> Python
    }
    return true;
}
#ifdef _WIN32
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
#else
bool fileExists(const std::string& p) {
    std::error_code ec;
    return std::filesystem::is_regular_file(p, ec);
}
#endif
}  // namespace

WorkerClient::~WorkerClient() { stop(); }

bool WorkerClient::start(std::string& err, const std::string& module) {
#ifdef __EMSCRIPTEN__
    (void)module;
    err = "WASM: Python worker not available (daemon mode only)";
    return false;
#else
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

#ifdef _WIN32
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
#else
    // POSIX: outPipe = child stdout -> parent; inPipe = parent -> child stdin.
    int outPipe[2], inPipe[2];
    if (pipe(outPipe) != 0 || pipe(inPipe) != 0) {
        err = std::string("pipe() failed: ") + std::strerror(errno);
        return false;
    }
    pid_t pid = fork();
    if (pid < 0) {
        err = std::string("fork failed: ") + std::strerror(errno);
        ::close(outPipe[0]); ::close(outPipe[1]);
        ::close(inPipe[0]); ::close(inPipe[1]);
        return false;
    }
    if (pid == 0) {
        // child: stdin <- inPipe[0], stdout -> outPipe[1]; stderr stays (host console).
        dup2(inPipe[0], STDIN_FILENO);
        dup2(outPipe[1], STDOUT_FILENO);
        ::close(inPipe[0]); ::close(inPipe[1]);
        ::close(outPipe[0]); ::close(outPipe[1]);
        if (!repo.empty() && chdir(repo.c_str()) != 0) { /* fall through; exec from cwd */ }
        execlp(python.c_str(), python.c_str(), "-m", module.c_str(), (char*)nullptr);
        _exit(127);  // exec failed
    }
    // parent: keep our ends, close the child's.
    ::close(inPipe[0]);
    ::close(outPipe[1]);
    fcntl(outPipe[0], F_SETFL, O_NONBLOCK);  // non-blocking reads so readLine can poll
    process_ = pid;
    childStdoutRd_ = outPipe[0];
    childStdinWr_ = inPipe[1];
    running_ = true;
    return true;
#endif  // _WIN32
#endif  // __EMSCRIPTEN__
}

void WorkerClient::stop() {
#ifdef __EMSCRIPTEN__
    running_ = false;
    return;
#elif defined(_WIN32)
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
#else
    if (childStdinWr_ >= 0) {
        const char* bye = "{\"id\":0,\"method\":\"shutdown\"}\n";
        ssize_t w = ::write(childStdinWr_, bye, std::strlen(bye));
        (void)w;
        ::close(childStdinWr_);
        childStdinWr_ = -1;
    }
    if (process_ >= 0) {
        // give the worker ~2s to exit on its own, then SIGTERM and reap.
        bool reaped = false;
        for (int i = 0; i < 200; ++i) {
            int status = 0;
            pid_t r = waitpid(process_, &status, WNOHANG);
            if (r == process_ || (r < 0 && errno == ECHILD)) { reaped = true; break; }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        if (!reaped) {
            ::kill(process_, SIGTERM);
            waitpid(process_, nullptr, 0);
        }
        process_ = -1;
    }
    if (childStdoutRd_ >= 0) {
        ::close(childStdoutRd_);
        childStdoutRd_ = -1;
    }
#endif
    running_ = false;
}

bool WorkerClient::readLine(std::string& line, int timeoutMs) {
#ifdef __EMSCRIPTEN__
    (void)line; (void)timeoutMs;
    return false;
#elif defined(_WIN32)
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
#else
    auto start = std::chrono::steady_clock::now();
    for (;;) {
        if (abort_.load()) return false;  // C39: teardown — stop polling immediately
        size_t nl = readBuf_.find('\n');
        if (nl != std::string::npos) {
            line = readBuf_.substr(0, nl);
            readBuf_.erase(0, nl + 1);
            return true;
        }
        auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                           std::chrono::steady_clock::now() - start)
                           .count();
        if ((int)elapsed > timeoutMs) return false;

        struct pollfd pfd;
        pfd.fd = childStdoutRd_;
        pfd.events = POLLIN;
        pfd.revents = 0;
        int pr = ::poll(&pfd, 1, 2);  // 2ms tick (matches the Windows fast-poll cadence)
        if (pr < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        if (pr == 0) {  // no data this tick — detect a child exit, else keep polling
            int status = 0;
            pid_t r = (process_ >= 0) ? waitpid(process_, &status, WNOHANG) : -1;
            if (r == process_ || (r < 0 && errno == ECHILD)) {
                process_ = -1;  // reaped here so stop() won't re-wait
                char chunk[4096];
                ssize_t got = ::read(childStdoutRd_, chunk, sizeof(chunk));  // final drain
                if (got > 0) {
                    readBuf_.append(chunk, (size_t)got);
                    continue;
                }
                return false;
            }
            continue;
        }
        char chunk[4096];
        ssize_t got = ::read(childStdoutRd_, chunk, sizeof(chunk));
        if (got > 0) {
            readBuf_.append(chunk, (size_t)got);
        } else if (got == 0) {
            return false;  // EOF: pipe closed
        } else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
            return false;
        }
    }
#endif  // platform
}

// T-G9 parity trace: with AI_CAD_RPC_TRACE=<file> set, every call() appends one JSONL
// record {method, params, ok, result|err}. This is the single funnel all panel RPCs pass
// through regardless of routing (Python worker subprocess, in-process libaicad doc/CAM
// core, ECAD/agent C++ paths), so tools/gui_parity/ can capture byte-comparable request/
// response streams from BOTH GUI binaries (host links services/worker directly too).
// Env unset (the default, incl. all production runs) = the wrapper is a tail call.
bool WorkerClient::call(const std::string& method, const json& params, json& result,
                        std::string& err, int timeoutMs, const EventFn& onEvent) {
    static const char* tracePath = std::getenv("AI_CAD_RPC_TRACE");
    if (!tracePath || !*tracePath)
        return callImpl(method, params, result, err, timeoutMs, onEvent);

    const bool ok = callImpl(method, params, result, err, timeoutMs, onEvent);
    try {
        // Redact credential-bearing fields recursively (agent_list_models et al carry
        // api_key/base_url auth material) — trace files must never hold live secrets.
        std::function<void(json&)> redact = [&](json& node) {
            if (node.is_object()) {
                for (auto& [k, v] : node.items()) {
                    if (k == "api_key" || k == "apikey" || k == "token" || k == "secret" ||
                        k == "password" || k == "authorization")
                        v = "<redacted>";
                    else
                        redact(v);
                }
            } else if (node.is_array()) {
                for (auto& v : node) redact(v);
            }
        };
        json rec = {{"method", method}, {"params", params}, {"ok", ok}};
        if (ok) rec["result"] = result;
        else    rec["err"] = err;
        redact(rec);
        static std::mutex traceMtx;  // two instances (doc + sim worker) share the file
        std::lock_guard<std::mutex> tlk(traceMtx);
        FILE* f = std::fopen(tracePath, "ab");
        if (f) {
            const std::string line = rec.dump() + "\n";
            std::fwrite(line.data(), 1, line.size(), f);
            std::fclose(f);
        }
    } catch (...) { /* tracing must never break the app */ }
    return ok;
}

bool WorkerClient::callAgentCancel(const std::string& runId, json& result, std::string& err) {
    return call("agent_cancel", json{{"run_id", runId}}, result, err, 5000);
}

bool WorkerClient::callImpl(const std::string& method, const json& params, json& result,
                            std::string& err, int timeoutMs, const EventFn& onEvent) {
    // agent_cancel must reach the engine WHILE an agent_run is blocked on this client.
    // Skip callMtx_ so Stop can flip the RunControl flag mid-provider-call. Concurrent
    // with the run's own call — safe because cancel only touches the process-wide registry
    // + atomic flag (no shared socket write on the in-flight agent RPC).
    const bool isCancel = (method == "agent_cancel");
    std::unique_lock<std::mutex> lk(callMtx_, std::defer_lock);
    if (!isCancel) lk.lock();
    if (abort_.load()) {  // C39: app is tearing down — intentional, no errorSink toast
        err = "aborted";
        return false;
    }
    // C++ AGENT routing (opt-in via AI_CAD_CPP_AGENT, default OFF). Translate the in-app
    // agent RPCs to the libaicad agent methods and run them in-process. Default OFF =>
    // routeAgentToCpp() returns false and this whole block is skipped (today's behavior).
    if (routeAgentToCpp(method, params)) {
        std::string cerr;
        json cppResult;
        bool ok = false;
        if (method == "agent_run_v2") {
            // {request, doc_id, provider?, model?, reasoning_effort?, ...} -> agent_run.
            // The C++ core resolves the provider from config.json unless the GUI pins a
            // provider NAME (or full descriptor) + model/effort overrides (picker O1).
            // CRITICAL: those top-level override fields MUST be forwarded — dropping them
            // makes the picker decorative and leaves ambient config.json/env in charge.
            json p = {{"request", params.value("request", std::string())},
                      {"doc_id", params.value("doc_id", std::string())}};
            for (const char* k : {"provider", "model", "reasoning_effort", "attachments",
                                  "context_refs"}) {
                auto it = params.find(k);
                if (it != params.end()) p[k] = *it;
            }
            // Pass through any run options the host may carry (budgets/run_id/control).
            json options = json::object();
            for (const char* k : {"options", "budgets", "run_id", "control"}) {
                auto it = params.find(k);
                if (it != params.end()) {
                    if (std::string(k) == "options" && it->is_object()) options = *it;
                    else options[k] = *it;
                }
            }
            if (!options.empty()) p["options"] = options;
            bool streamedLive = false;
            ok = callAgentStreaming("agent_run", p, cppResult, cerr, timeoutMs, onEvent,
                                    streamedLive);
            // Batched replay ONLY when events did NOT stream live (old engine w/o the sink export).
            // When streamedLive, onEvent already fired per-event during the run — replaying the
            // batched cppResult["events"] would double every card.
            if (!streamedLive && ok && onEvent && cppResult.is_object() &&
                cppResult.contains("events") && cppResult["events"].is_array()) {
                for (const auto& ev : cppResult["events"]) onEvent(ev);
            }
        } else if (method == "agent_mission_v1") {
            // {request, doc_id, provider?, model?, ...} -> agent_mission_v1. Same O1
            // passthrough as agent_run_v2 so the picker beats ambient config.
            json p = {{"request", params.value("request", std::string())},
                      {"doc_id", params.value("doc_id", std::string())}};
            for (const char* k : {"provider", "model", "reasoning_effort", "attachments",
                                  "context_refs"}) {
                auto it = params.find(k);
                if (it != params.end()) p[k] = *it;
            }
            json options = json::object();
            for (const char* k : {"options", "budgets", "run_id", "control"}) {
                auto it = params.find(k);
                if (it != params.end()) {
                    if (std::string(k) == "options" && it->is_object()) options = *it;
                    else options[k] = *it;
                }
            }
            if (!options.empty()) p["options"] = options;
            bool streamedLive = false;
            ok = callAgentStreaming("agent_mission_v1", p, cppResult, cerr, timeoutMs, onEvent,
                                    streamedLive);
            if (!streamedLive && ok && onEvent && cppResult.is_object() &&
                cppResult.contains("events") && cppResult["events"].is_array()) {
                for (const auto& ev : cppResult["events"]) onEvent(ev);
            }
        } else if (method == "agent_ask_v2") {
            // {question, doc_id, provider?, model?, ...} -> agent_answer_question.
            json p = {{"question", params.value("question", std::string())},
                      {"doc_id", params.value("doc_id", std::string())}};
            for (const char* k : {"provider", "model", "reasoning_effort"}) {
                auto it = params.find(k);
                if (it != params.end()) p[k] = *it;
            }
            ok = engine::call("agent_answer_question", p, cppResult, cerr, timeoutMs);
        } else if (method == "agent_clarify_v1") {
            // {request, doc_id, provider?, model?, ...} -> agent_clarify.
            json p = {{"request", params.value("request", std::string())},
                      {"doc_id", params.value("doc_id", std::string())}};
            for (const char* k : {"provider", "model", "reasoning_effort"}) {
                auto it = params.find(k);
                if (it != params.end()) p[k] = *it;
            }
            ok = engine::call("agent_clarify", p, cppResult, cerr, timeoutMs);
        } else if (method == "agent_pm_assist_v1") {
            // The host's PM payload (doc_id, intent, form_schema, current, selected_refs)
            // IS the params object the C++ assist expects under "params".
            json p = {{"params", params}, {"doc_id", params.value("doc_id", std::string())}};
            ok = engine::call("agent_pm_assist", p, cppResult, cerr, timeoutMs);
            // C++ returns {validated:{...}}; the host consumes the assist result object
            // directly (patches/activate_well/...). Unwrap so the caller sees the same
            // shape the Python agent_pm_assist_v1 returns.
            if (ok && cppResult.is_object() && cppResult.contains("validated"))
                cppResult = cppResult["validated"];
        } else if (method == "get_prompts_v2") {
            // -> agent_get_prompts_v2 {} (C++ default/env resolves the prompts dir).
            ok = engine::call("agent_get_prompts_v2", json::object(), cppResult, cerr,
                              timeoutMs);
        } else if (method == "agent_list_models") {
            // {provider, base_url?, api_key?} -> {ok, models:[...]} (pass-through probe).
            ok = engine::call("agent_list_models", params, cppResult, cerr, timeoutMs);
        } else if (method == "agent_simulate_v1") {
            // {doc_id, request} -> {ok, answer, drag_n, lift_n, ...}: run a wind-tunnel CFD on the
            // current geometry (pass-through; the core parses the flow condition + runs LBM).
            ok = engine::call("agent_simulate_v1", params, cppResult, cerr, timeoutMs);
        } else if (method == "agent_cancel") {
            // Best-effort stop. In-process: call libaicad directly (no mutex contention with
            // the blocked agent_run). Daemon: open a SHORT-LIVED second EngineClient so we
            // don't queue behind the in-flight agent_run on the shared socket.
            if (engine::daemonMode()) {
                aicad_ipc::EngineClient side;
                aicad_ipc::EngineClient::Options opts;
                opts.clientKind = "gui";
                opts.allowSpawn = false;  // never spawn a new hostd for cancel
                std::string cerr2;
                if (side.connectOrSpawn(opts, cerr2)) {
                    ok = side.call("agent_cancel", params, cppResult, cerr, 5000);
                } else {
                    cerr = cerr2.empty() ? "cancel: daemon unreachable" : cerr2;
                    ok = false;
                }
            } else {
                ok = engine::call("agent_cancel", params, cppResult, cerr, timeoutMs);
            }
        }
        if (ok) {
            result = std::move(cppResult);
            return true;
        }
        // C++ reported a structured error: surface it like the doc path (round-trip OK).
        if (cppResult.is_object() && cppResult.contains("code")) {
            result = cppResult;
            err = cppResult.value("message", std::string("cpp-agent error"));
            return true;
        }
        // Hard failure (a C++ throw, not a structured {code} envelope). The v2 agent RPCs
        // (agent_run_v2 / ask / clarify / pm_assist / simulate) are C++-ONLY — the Python
        // worker deleted them (engine/worker.py: "MIGRATED TO THE C++ CORE"), so falling
        // through to it would ALWAYS return the misleading "unknown method: agent_run_v2"
        // and bury the real cause (e.g. a provider/config error). Surface the actual C++
        // error instead. (agent_list_models is the one exception: it has a host-side static
        // catalog fallback, so we let it fall through and degrade gracefully.)
        if (method != "agent_list_models") {
            err = cerr.empty() ? ("cpp-agent (" + method + ") failed") : cerr;
            if (errorSink_) errorSink_(method, err);
            return false;
        }
        // else: fall through to the Python worker path below (list_models tolerates failure).
    }
    // C++ SIM (FEA) routing (opt-in via AI_CAD_CPP_SIM, default OFF). Run the FEA mesh /
    // static / modal RPCs in-process against libaicad. The host already sends the C++
    // {study:{...}} contract verbatim, so params pass through unchanged; the result carries
    // summary + overlay_aicm (what sim_panel reads). Default OFF => routeSimToCpp() returns
    // false and this block is skipped (byte-for-byte today's Python sim path).
    if (routeSimToCpp(method, params)) {
        std::string cerr;
        json cppResult;
        if (engine::call(method, params, cppResult, cerr, timeoutMs)) {
            result = std::move(cppResult);  // ok: result holds {summary, overlay_aicm, ...}
            return true;
        }
        // The core reported a STRUCTURED error envelope ({code,message,detail}). Surface it
        // the same way the doc/agent seam does: deliver it in `result` and return true (the
        // round-trip succeeded, the METHOD reported an error). SimClient::launch reads
        // result.contains("code") to mark the job failed with the message.
        if (cppResult.is_object() && cppResult.contains("code")) {
            result = std::move(cppResult);
            err = result.value("message", std::string("cpp-sim error"));
            return true;
        }
        // Hard failure (DLL throw / load race after the available() check): do NOT hard-break
        // sim — fall through to the Python sim worker path below so analysis keeps working.
    }
    // C++ ECAD routing (opt-in via AI_CAD_CPP_ECAD, default OFF). MIRRORS the doc_* seam
    // below: an ecad_new/ecad_open mints an "ecadcpp" doc and binds the whole doc lifetime
    // to C++; any later ecad_* carrying that ecadcpp doc_id routes here. Default OFF =>
    // routeEcadToCpp() returns false (and no ecadcpp doc exists) so this block is skipped
    // entirely — byte-for-byte today's Python ECAD path. No params translation: the host's
    // ecad_* send-shape IS the libaicad contract (kind/name, doc_id, base_revision, ops...).
    if (routeEcadToCpp(method, params)) {
        // ecad_new / ecad_open MINT a new ECAD doc; everything else operates on an
        // existing ecadcpp doc (no Python doc to fall back to => fail loud on hard error).
        const bool minting = (method == "ecad_new" || method == "ecad_open");
        std::string cerr;
        if (engine::call(method, params, result, cerr, timeoutMs))
            return true;  // handled (rc==0); `result` holds {doc_id, revision, tree, ...}
        // Nonzero rc: a structured OpError carries {code,message,detail} in `result`.
        if (result.is_object() && result.contains("code")) {
            // A MINTING call the core declined as "unsupported" created no C++ doc — fall
            // back to the Python worker instead of surfacing the error (no engine switch).
            if (!(minting && result.value("code", std::string()) == "unsupported")) {
                err = result.value("message", std::string("cpp-ecad error"));
                return true;  // structured error reported (round-trip OK)
            }
            // else: fall through to the Python worker path below
        } else if (!minting) {
            // Hard failure (DLL throw, or an unported ecad_* like ecad_fill_zones /
            // ecad_simulate / doc-id ecad_autoroute) on a call bound to an EXISTING
            // ecadcpp doc — there is no Python doc to fall back to, so fail loudly.
            err = "cpp-ecad (" + method + "): " + cerr;
            if (errorSink_) errorSink_(method, err);
            return false;
        }
        // Minting-call fallback: the core could not mint this doc (unsupported kind / the
        // DLL is unavailable) — continue to the Python worker so ECAD creation never
        // hard-breaks (it mints a Python doc id, which keeps the doc on Python).
    }
    // CAM (computer-aided machining): every cam_* RPC lives ONLY in the C++ core
    // (libaicad); there is no Python CAM engine. Route them all to cpp_core and report
    // its result (an OpError surfaces as {code,message,detail}); never fall back to Python.
    if (method.rfind("cam_", 0) == 0) {
        std::string cerr;
        if (engine::call(method, params, result, cerr, timeoutMs))
            return true;  // handled (rc==0)
        if (result.is_object() && result.contains("code")) {
            err = result.value("message", std::string("cpp-cam error"));
            return true;  // structured OpError round-tripped
        }
        err = "cpp-cam (" + method + "): " + cerr;
        if (errorSink_) errorSink_(method, err);
        return false;
    }
    // P16: route C++-backed documents to the in-process core (libaicad) instead of the
    // Python worker. All-or-nothing per document (NO mid-doc Python fallback — Python
    // never created this doc), so an unported method surfaces a clear error rather than
    // silently misrouting. The diagnostic parity probe in doc_mirror is unaffected.
    if (routeDocToCpp(method, params)) {
        const bool minting = isMintingMethod(method, params);
        std::string cerr;
        if (engine::call(method, params, result, cerr, timeoutMs))
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
        } else if (engine::available()) {
            // MINTING call failed with an UNSTRUCTURED core error while the DLL is
            // loaded (e.g. doc_open: "no such file: live.step+"). That is a real
            // failure, not an "unsupported container" decline — falling through to
            // the Python worker would mask it behind "unknown method: doc_open"
            // (the oracle has no doc engine anymore). Surface the core's message.
            err = cerr.empty() ? ("cpp-core (" + method + ") failed") : cerr;
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
#if defined(__EMSCRIPTEN__)
    (void)line;
    err = "WASM: worker pipe write not available";
    if (errorSink_) errorSink_(method, err);
    return false;
#else
#ifdef _WIN32
    DWORD wrote = 0;
    if (!WriteFile(childStdinWr_, line.data(), (DWORD)line.size(), &wrote, nullptr)) {
#else
    if (::write(childStdinWr_, line.data(), line.size()) != (ssize_t)line.size()) {
#endif
        err = "write failed";
        if (errorSink_) errorSink_(method, err);  // W1-B
        return false;
    }
#endif  // !__EMSCRIPTEN__
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
            if (method == "doc_list" && engine::available() &&
                result.is_object() && result.contains("docs") && result["docs"].is_array()) {
                json cppList;
                std::string cerr;
                if (engine::call("doc_list", json::object(), cppList, cerr, timeoutMs) &&
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
