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
        for (const char* key : {"doc_id", "into"}) {
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
        return core::cpp_core().ensureLoaded(lerr);
    }
    if (!cppAgentFlag()) return false;
    if (!(method == "agent_run_v2" || method == "agent_ask_v2" ||
          method == "agent_clarify_v1" || method == "agent_pm_assist_v1" ||
          method == "agent_simulate_v1" || method == "get_prompts_v2"))
        return false;  // agent_design (legacy) + everything else stays Python
    if (!agentDocIsCpp(method, params)) return false;  // only C++-backed docs (or doc-indep)
    // Ensure the core DLL is loadable before committing to the C++ path. ensureLoaded is
    // idempotent (loads once); if it can't load, fall through to the Python worker so the
    // agent never hard-breaks. For a doc-cpp doc the DLL is already loaded (it minted the
    // doc); for get_prompts_v2 this triggers the lazy load on first use.
    std::string lerr;
    return core::cpp_core().ensureLoaded(lerr);
}
// --- C++ SIM (FEA) routing (opt-in, default OFF) ----------------------------
// Route the host's FEA simulation RPCs (sim_mesh / sim_run_static / sim_run_modal)
// to the pure-C++ core (libaicad) instead of the Python sim worker. MIRRORS the
// agent seam but is DEFAULT OFF: unless AI_CAD_CPP_SIM is set (and != "0") every sim
// call goes to the Python sim worker exactly as today. sim_run_flow (LBM/CFD) and
// sim_cancel are NEVER routed — they stay Python.
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
          method == "sim_run_fatigue"))
        return false;  // sim_run_flow (LBM) + sim_cancel + everything else stays Python
    if (!core::cpp_core().available()) {
        // Lazily try to load the core DLL; if it can't load, fall through to Python so
        // sim never hard-breaks. ensureLoaded is idempotent (loads once).
        std::string lerr;
        if (!core::cpp_core().ensureLoaded(lerr)) return false;
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
    return f && f[0] && f[0] != '0';  // explicit opt-in only (default OFF)
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
    if (!core::cpp_core().available()) {
        std::string lerr;
        if (!core::cpp_core().ensureLoaded(lerr)) return false;  // can't load -> Python
    }
    return true;
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
    // C++ AGENT routing (opt-in via AI_CAD_CPP_AGENT, default OFF). Translate the in-app
    // agent RPCs to the libaicad agent methods and run them in-process. Default OFF =>
    // routeAgentToCpp() returns false and this whole block is skipped (today's behavior).
    if (routeAgentToCpp(method, params)) {
        std::string cerr;
        json cppResult;
        bool ok = false;
        if (method == "agent_run_v2") {
            // {request, doc_id, ...} -> agent_run {request, doc_id, options:<passthrough>}.
            // The C++ core resolves the provider from config.json and picks micro vs design.
            json p = {{"request", params.value("request", std::string())},
                      {"doc_id", params.value("doc_id", std::string())}};
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
            ok = core::cpp_core().call("agent_run", p, cppResult, cerr);
            // Replay captured events through onEvent so the Copilot/Activity pane updates.
            // (Batched after the run for this opt-in v1 — live streaming is a later step.)
            if (ok && onEvent && cppResult.is_object() && cppResult.contains("events") &&
                cppResult["events"].is_array()) {
                for (const auto& ev : cppResult["events"]) onEvent(ev);
            }
        } else if (method == "agent_ask_v2") {
            // {question, doc_id, ...} -> agent_answer_question {question, doc_id}.
            json p = {{"question", params.value("question", std::string())},
                      {"doc_id", params.value("doc_id", std::string())}};
            ok = core::cpp_core().call("agent_answer_question", p, cppResult, cerr);
        } else if (method == "agent_clarify_v1") {
            // {request, doc_id, ...} -> agent_clarify {request, doc_id}. Returns
            // {ready, questions:[{key,ask,unit,suggested}], assumptions:[...]}.
            json p = {{"request", params.value("request", std::string())},
                      {"doc_id", params.value("doc_id", std::string())}};
            ok = core::cpp_core().call("agent_clarify", p, cppResult, cerr);
        } else if (method == "agent_pm_assist_v1") {
            // The host's PM payload (doc_id, intent, form_schema, current, selected_refs)
            // IS the params object the C++ assist expects under "params".
            json p = {{"params", params}, {"doc_id", params.value("doc_id", std::string())}};
            ok = core::cpp_core().call("agent_pm_assist", p, cppResult, cerr);
            // C++ returns {validated:{...}}; the host consumes the assist result object
            // directly (patches/activate_well/...). Unwrap so the caller sees the same
            // shape the Python agent_pm_assist_v1 returns.
            if (ok && cppResult.is_object() && cppResult.contains("validated"))
                cppResult = cppResult["validated"];
        } else if (method == "get_prompts_v2") {
            // -> agent_get_prompts_v2 {} (C++ default/env resolves the prompts dir).
            ok = core::cpp_core().call("agent_get_prompts_v2", json::object(), cppResult, cerr);
        } else if (method == "agent_list_models") {
            // {provider, base_url?, api_key?} -> {ok, models:[...]} (pass-through probe).
            ok = core::cpp_core().call("agent_list_models", params, cppResult, cerr);
        } else if (method == "agent_simulate_v1") {
            // {doc_id, request} -> {ok, answer, drag_n, lift_n, ...}: run a wind-tunnel CFD on the
            // current geometry (pass-through; the core parses the flow condition + runs LBM).
            ok = core::cpp_core().call("agent_simulate_v1", params, cppResult, cerr);
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
        // Hard failure (DLL throw / load race): do NOT hard-break the agent — fall through
        // to the Python worker path below so the in-app AI keeps working.
    }
    // C++ SIM (FEA) routing (opt-in via AI_CAD_CPP_SIM, default OFF). Run the FEA mesh /
    // static / modal RPCs in-process against libaicad. The host already sends the C++
    // {study:{...}} contract verbatim, so params pass through unchanged; the result carries
    // summary + overlay_aicm (what sim_panel reads). Default OFF => routeSimToCpp() returns
    // false and this block is skipped (byte-for-byte today's Python sim path).
    if (routeSimToCpp(method, params)) {
        std::string cerr;
        json cppResult;
        if (core::cpp_core().call(method, params, cppResult, cerr)) {
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
        if (core::cpp_core().call(method, params, result, cerr))
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
