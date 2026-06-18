// libaicad C ABI implementation — see libaicad.h.
//
// Dispatches string-in/string-out JSON calls to the C++ core. Today it serves:
//   ping             liveness + ABI echo (no kernel work; used by the host to
//                    confirm the DLL loaded and its OCCT deps resolved).
//   doc_compile_cpp  DIAGNOSTIC compile: takes a full document feature tree +
//                    env, runs aicad::doc::compile_document, writes the surviving
//                    bodies to an AICM file, and returns the per-feature statuses
//                    + body count + the AICM path. This is the strangler's
//                    parity probe (Step 2 of docs/CPP_MIGRATION.md): the host can
//                    feed the SAME tree it sends to the Python worker and diff the
//                    results, WITHOUT changing the production Python path.
//
// Everything here is exception-safe: any throw becomes a nonzero return with an
// {"error":...} payload so a bug in the C++ core can never crash the host.

#include "api/libaicad.h"

#include <array>
#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <exception>
#include <filesystem>
#include <fstream>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "agent/ask.h"
#include "agent/config.h"
#include "agent/events.h"
#include "agent/machine.h"
#include "agent/micro.h"
#include "agent/pm_assist.h"
#include "agent/paths.h"
#include "agent/ops_emit.h"
#include "agent/providers.h"
#include "agent/roles.h"
#include "agent/run.h"
#include "agent/state.h"
#include "doc/document.h"
#include "doc/export2d.h"
#include "doc/drawing_session.h"
#include "doc/session.h"
#include "doc/validate.h"
#include "ecad/autoroute.h"
#include "ecad/recipes.h"
#include "ecad/schema.h"
#include "ecad/session.h"
#include "geom/aicm.h"
#include "geom/ecad_3d.h"
#include "sim/fea_modal.h"
#include "sim/fea_run.h"
#include "sim/fea_static.h"
#include "sim/lbm.h"
#include "sim/mesher.h"
#include "sim/netlist.h"
#include "sim/numpy_rng.h"
#include "sim/overlay.h"
#include "sim/spice.h"

using nlohmann::json;

namespace {

// malloc a NUL-terminated copy of `s` so the host frees it with aicad_free()
// (std::free). Returns nullptr only on allocation failure.
char* dup_cstr(const std::string& s) {
    char* p = static_cast<char*>(std::malloc(s.size() + 1));
    if (!p) return nullptr;
    std::memcpy(p, s.data(), s.size());
    p[s.size()] = '\0';
    return p;
}

// Emit `j` into *out_json (heap) and return `rc`. Always leaves *out_json valid.
int emit(json&& j, char** out_json, int rc) {
    if (out_json) *out_json = dup_cstr(j.dump());
    return rc;
}

int emit_error(const std::string& msg, char** out_json) {
    return emit(json{{"error", msg}}, out_json, 1);
}

// Flatten a JSON numeric array — accepts nested ([[x,y,z],...]) OR already-flat
// ([x,y,z,...]) — into a flat double vector. Used by the overlay/map handlers.
std::vector<double> flat_doubles(const json& j) {
    std::vector<double> v;
    if (!j.is_array()) return v;
    for (const auto& x : j) {
        if (x.is_array()) {
            for (const auto& c : x) v.push_back(c.get<double>());
        } else {
            v.push_back(x.get<double>());
        }
    }
    return v;
}

// Hex-encode a byte blob (lowercase) for the chunk-bytes diagnostic handlers.
std::string to_hex(const std::vector<uint8_t>& b) {
    static const char* hexd = "0123456789abcdef";
    std::string s;
    s.reserve(b.size() * 2);
    for (uint8_t c : b) {
        s.push_back(hexd[c >> 4]);
        s.push_back(hexd[c & 0xF]);
    }
    return s;
}

// Choose where to write the AICM bytes. Honour an explicit params["aicm_out"]
// (the host/tests can pin a path), else a unique file in the OS temp dir. The
// counter + pid keep concurrent compiles from colliding without needing a clock
// (Date/now is intentionally avoided elsewhere in the codebase).
std::filesystem::path aicm_target(const json& params) {
    if (params.contains("aicm_out") && params["aicm_out"].is_string()) {
        return std::filesystem::path(params["aicm_out"].get<std::string>());
    }
    static std::atomic<unsigned long long> seq{0};
    unsigned long long n = seq.fetch_add(1);
    std::filesystem::path dir = std::filesystem::temp_directory_path();
    return dir / ("aicad_cpp_" + std::to_string(n) + ".aicm");
}

// doc_compile_cpp: compile a document tree to AICM + statuses (diagnostic).
int handle_compile(const json& params, char** out_json) {
    // The tree may arrive under "doc" (the {"features":[...]} shape compile_document
    // wants) or, for convenience, the host's tree may be passed directly. Accept
    // either: if there's a top-level "features", treat params itself as the doc.
    json doc;
    if (params.contains("doc")) {
        doc = params["doc"];
    } else if (params.contains("features")) {
        doc = params;
    } else {
        return emit_error("doc_compile_cpp: params need a 'doc' object (or a top-level 'features')", out_json);
    }
    json env = params.contains("env") ? params["env"] : json::object();

    aicad::doc::CompileResult res = aicad::doc::compile_document(doc, env);

    json statuses = json::array();
    for (const auto& s : res.statuses) {
        statuses.push_back(json{
            {"id", s.id},
            {"type", s.type},
            {"ok", s.ok},
            {"error", s.error},
        });
    }

    std::string aicm_path;
    if (!res.aicm.empty()) {
        std::filesystem::path target = aicm_target(params);
        std::ofstream f(target, std::ios::binary | std::ios::trunc);
        if (!f) {
            return emit_error("doc_compile_cpp: cannot open AICM output: " + target.string(), out_json);
        }
        f.write(reinterpret_cast<const char*>(res.aicm.data()),
                static_cast<std::streamsize>(res.aicm.size()));
        f.close();
        aicm_path = std::filesystem::absolute(target).string();
    }

    // Per-surviving-body stats (surfacing parity gate: area/face/edge counts for open
    // shells, volume for solids). Additive — existing volume fixtures are unaffected.
    json bodies = json::array();
    for (const auto& b : res.bodies_info) {
        bodies.push_back(json{
            {"name", b.name},
            {"kind", b.kind},
            {"volume", b.volume},
            {"area", b.area},
            {"n_faces", b.n_faces},
            {"n_edges", b.n_edges},
        });
    }

    return emit(json{
                    {"ok", res.ok},
                    {"body_count", res.body_count},
                    {"volume", res.volume},
                    {"bodies", bodies},
                    {"statuses", statuses},
                    {"owner_map", res.owner_map.is_null() ? json::object() : res.owner_map},
                    {"aicm_path", aicm_path},
                    {"aicm_bytes", static_cast<uint64_t>(res.aicm.size())},
                },
                out_json, 0);
}

// P54b host-ergonomics: when an assist caller (agent_answer_question / agent_pm_assist)
// passes a live C++ doc_id but omits the explicit doc / revision / feature_status, read
// them from that live session so the host can send just {question|params, doc_id}. The
// existing explicit-params path (the gates pass doc/revision/feature_status inline) is
// untouched: each field is filled ONLY when absent. doc_id "?" / empty / not a live
// session leaves everything as-is (the gate behavior). Best-effort — any failure to read
// the session is swallowed so the assist still runs from whatever was provided.
//   `op` is the ordered_json params; mutated in place. `doc_id` is the resolved id.
void hydrate_assist_from_session(nlohmann::ordered_json& op, const std::string& doc_id) {
    using ojson = nlohmann::ordered_json;
    if (doc_id.empty() || doc_id == "?") return;
    const bool need_doc = !op.contains("doc") || op["doc"].is_null();
    const bool need_rev = !(op.contains("revision") && op["revision"].is_number());
    const bool need_fs = !op.contains("feature_status");
    if (!need_doc && !need_rev && !need_fs) return;
    try {
        // doc_tree -> {revision, tree}. Throws OpError for an unknown doc.
        json t = aicad::doc::doc_tree(doc_id);
        if (need_doc && t.contains("tree"))
            op["doc"] = ojson::parse(t["tree"].dump());
        if (need_rev && t.contains("revision") && t["revision"].is_number())
            op["revision"] = t["revision"].get<long>();
        if (need_fs) {
            // feature_status lives in the rebuild response (doc_rebuild forces a
            // recompile without bumping the revision).
            json rb = aicad::doc::doc_rebuild(doc_id);
            if (rb.contains("rebuild") && rb["rebuild"].contains("feature_status"))
                op["feature_status"] = ojson::parse(rb["rebuild"]["feature_status"].dump());
        }
    } catch (...) {
        // Unknown/closed doc or read failure: leave op untouched (run from provided data).
    }
}

// P54b host-ergonomics: resolve the assist provider from the ambient config.json when
// the caller did not pass one (the host sends bare {question|params, doc_id}). Returns
// the descriptor to feed make_assist_provider. On a config-provider refusal/resolution
// failure, returns {error:...} (the caller may surface it). The gates always pass an
// explicit provider, so this fallback never runs under them.
nlohmann::json resolve_assist_provider(const nlohmann::json& params) {
    if (params.contains("provider") && !params["provider"].is_null())
        return params["provider"];  // explicit (gate mock / host override)
    json cfg = aicad::agent::load_config();
    return aicad::agent::resolve_provider(std::nullopt, cfg, nlohmann::json::object());
}

}  // namespace

extern "C" {

AICAD_API const char* aicad_abi_version(void) { return "1"; }

AICAD_API void aicad_free(char* s) { std::free(s); }

AICAD_API int aicad_call(const char* method, const char* params_json, char** out_json) {
    if (out_json) *out_json = nullptr;
    if (!method) return emit_error("null method", out_json);

    std::string m(method);
    json params = json::object();
    try {
        if (params_json && params_json[0] != '\0') {
            params = json::parse(params_json);
        }
    } catch (const std::exception& e) {
        return emit_error(std::string("invalid params JSON: ") + e.what(), out_json);
    }

    try {
        if (m == "ping") {
            return emit(json{{"ok", true}, {"abi", aicad_abi_version()}, {"echo", params}},
                        out_json, 0);
        }
        if (m == "doc_compile_cpp") {
            return handle_compile(params, out_json);
        }
        // --- P51: agent/LLM provider layer diagnostics (no live network) -------
        // These let the Python parity gate inspect request construction + response
        // parsing of the C++ provider port WITHOUT a live POST.
        if (m == "agent_build_request") {
            // {provider, model?, system?, messages, max_tokens?, base_url?} ->
            // {url, headers, body}. Pure (no network). headers as an object.
            std::string provider = params.value("provider", std::string());
            std::string system = params.value("system", std::string());
            int max_tokens = params.value("max_tokens", 8000);
            json messages = params.contains("messages") ? params["messages"] : json::array();
            aicad::agent::BuiltRequest req;
            if (provider == "anthropic") {
                std::string model = params.value("model",
                                                 std::string(aicad::agent::kAnthropicDefaultModel));
                req = aicad::agent::anthropic_build_request(model, system, messages, max_tokens);
            } else if (provider == "openai" || provider == "llamacpp") {
                std::string def = provider == "llamacpp" ? std::string("local-gguf")
                                                         : std::string(aicad::agent::kOpenAIDefaultModel);
                std::string model = params.value("model", def);
                std::string base_url = params.value("base_url", std::string());
                req = aicad::agent::openai_build_request(model, system, messages, max_tokens,
                                                         base_url);
            } else {
                return emit_error("agent_build_request: unknown provider: " + provider, out_json);
            }
            json headers = json::object();
            for (const auto& h : req.headers) headers[h.first] = h.second;
            return emit(json{{"url", req.url}, {"headers", headers}, {"body", req.body_json}},
                        out_json, 0);
        }
        if (m == "agent_parse_response") {
            // {provider, response} -> {text, thinking?}.
            std::string provider = params.value("provider", std::string());
            json resp = params.contains("response") ? params["response"] : json::object();
            if (provider == "anthropic") {
                aicad::agent::ParsedReply pr = aicad::agent::anthropic_parse_response(resp);
                return emit(json{{"text", pr.text}, {"thinking", pr.thinking}}, out_json, 0);
            }
            if (provider == "openai" || provider == "llamacpp") {
                aicad::agent::ParsedReply pr = aicad::agent::openai_parse_response(resp);
                return emit(json{{"text", pr.text}}, out_json, 0);
            }
            return emit_error("agent_parse_response: unknown provider: " + provider, out_json);
        }
        if (m == "agent_b64_file") {
            // {path} -> {b64}. base64.standard_b64encode of the file bytes.
            std::string path = params.value("path", std::string());
            std::string b64 = aicad::agent::base64_encode(aicad::agent::read_file_bytes(path));
            return emit(json{{"b64", b64}}, out_json, 0);
        }
        if (m == "agent_normalize_base_url") {
            // {base_url} -> {base_url} (the normalized form). Pure helper probe.
            std::string base_url = params.value("base_url", std::string());
            return emit(json{{"base_url", aicad::agent::openai_normalize_base_url(base_url)}},
                        out_json, 0);
        }
        if (m == "agent_complete") {
            // {provider, cfg?, system?, messages?, max_tokens?} -> {text, thinking, error}.
            // For provider:"mock" this is fully offline (scripted replies in cfg.replies).
            // For anthropic/openai/llamacpp it performs a LIVE http_post (the gate only
            // drives this when an API key is present).
            std::string provider = params.value("provider", std::string());
            json cfg = params.contains("cfg") ? params["cfg"] : json::object();
            std::string system = params.value("system", std::string());
            json messages = params.contains("messages") ? params["messages"] : json::array();
            int max_tokens = params.value("max_tokens", 8000);
            aicad::agent::CompleteResult cr =
                aicad::agent::complete(provider, cfg, system, messages, max_tokens);
            return emit(json{{"text", cr.text}, {"thinking", cr.thinking}, {"error", cr.error}},
                        out_json, cr.error.empty() ? 0 : 0);
        }
        if (m == "agent_role_of") {
            // {system} -> {role}. RoleScriptedProvider role sniffer probe.
            std::string system = params.value("system", std::string());
            return emit(json{{"role", aicad::agent::RoleScriptedProvider::role_of(system)}},
                        out_json, 0);
        }
        // --- P52: agent SUPPORT layer diagnostics (paths/config/ops/events/roles) ---
        // All PURE string/file-IO ports, gated byte-for-byte vs the Python oracle.
        if (m == "agent_extract_ops") {
            // {reply} -> {ops|null, error|null}.
            std::string reply = params.value("reply", std::string());
            aicad::agent::OpsResult r = aicad::agent::extract_ops(reply);
            return emit(json{{"ops", r.ops ? *r.ops : json(nullptr)},
                             {"error", r.error ? json(*r.error) : json(nullptr)}},
                        out_json, 0);
        }
        if (m == "agent_extract_revert") {
            std::string reply = params.value("reply", std::string());
            return emit(json{{"revert", aicad::agent::extract_revert(reply)}}, out_json, 0);
        }
        if (m == "agent_extract_script") {
            std::string reply = params.value("reply", std::string());
            auto s = aicad::agent::extract_script(reply);
            return emit(json{{"script", s ? json(*s) : json(nullptr)}}, out_json, 0);
        }
        if (m == "agent_first_balanced") {
            // {text, open, close} -> {span|null}.
            std::string text = params.value("text", std::string());
            std::string open = params.value("open", std::string("["));
            std::string close = params.value("close", std::string("]"));
            auto s = aicad::agent::first_balanced(text, open.empty() ? '[' : open[0],
                                                  close.empty() ? ']' : close[0]);
            return emit(json{{"span", s ? json(*s) : json(nullptr)}}, out_json, 0);
        }
        if (m == "agent_script_escape_op") {
            std::string code = params.value("code", std::string());
            std::string name = params.value("name", std::string("Script"));
            return emit(aicad::agent::script_escape_op(code, name), out_json, 0);
        }
        if (m == "agent_offline_safe") {
            json provider = params.contains("provider") ? params["provider"] : json::object();
            return emit(json{{"safe", aicad::agent::provider_offline_safe(provider)}},
                        out_json, 0);
        }
        if (m == "agent_host_is_loopback") {
            std::string base_url = params.value("base_url", std::string());
            return emit(json{{"loopback", aicad::agent::host_is_loopback(base_url)}},
                        out_json, 0);
        }
        if (m == "agent_resolve_provider") {
            // {name?, cfg, env:{...}} -> resolved descriptor or {error}.
            std::optional<std::string> name;
            if (params.contains("name") && params["name"].is_string())
                name = params["name"].get<std::string>();
            json cfg = params.contains("cfg") ? params["cfg"] : json::object();
            json env = params.contains("env") ? params["env"] : json::object();
            return emit(aicad::agent::resolve_provider(name, cfg, env), out_json, 0);
        }
        if (m == "agent_cap_raw") {
            std::string text = params.value("text", std::string());
            return emit(json{{"capped", aicad::agent::cap_raw(text)}}, out_json, 0);
        }
        if (m == "agent_run_event") {
            // {run_id, kind, data} -> the emitted event (also persisted to jsonl).
            std::string run_id = params.value("run_id", std::string());
            std::string kind = params.value("kind", std::string());
            json data = params.contains("data") ? params["data"] : json::object();
            aicad::agent::RunEvents ev(run_id);
            // NOTE: a fresh RunEvents truncates events.jsonl on construct. The gate
            // drives a multi-event run via agent_run_events_batch (below) instead.
            return emit(ev.emit(kind, data), out_json, 0);
        }
        if (m == "agent_run_events_batch") {
            // {run_id, events:[{kind, data}]} -> [emitted events]. One RunEvents
            // (one truncate), then emit each — the realistic multi-event sequence.
            std::string run_id = params.value("run_id", std::string());
            aicad::agent::RunEvents ev(run_id);
            json out = json::array();
            if (params.contains("events") && params["events"].is_array())
                for (const auto& e : params["events"]) {
                    std::string kind = e.value("kind", std::string());
                    json data = e.contains("data") ? e["data"] : json::object();
                    out.push_back(ev.emit(kind, data));
                }
            return emit(std::move(out), out_json, 0);
        }
        if (m == "agent_run_events_read") {
            // {run_id} -> [parsed events] read back from events.jsonl.
            std::string run_id = params.value("run_id", std::string());
            std::filesystem::path p =
                aicad::agent::paths_cache() / "runs" / run_id / "events.jsonl";
            json out = json::array();
            std::ifstream fh(p, std::ios::binary);
            std::string line;
            while (std::getline(fh, line)) {
                if (!line.empty() && line.back() == '\r') line.pop_back();
                if (line.empty()) continue;
                out.push_back(json::parse(line));
            }
            return emit(std::move(out), out_json, 0);
        }
        if (m == "agent_load_prompt") {
            std::string role = params.value("role", std::string());
            std::string pd = params.value("prompts_dir", std::string());
            auto pr = aicad::agent::load_prompt(role, pd);
            return emit(json{{"text", pr.first}, {"source", pr.second}}, out_json, 0);
        }
        if (m == "agent_prompt_hash") {
            std::string text = params.value("text", std::string());
            return emit(json{{"hash", aicad::agent::prompt_hash(text)}}, out_json, 0);
        }
        if (m == "agent_get_prompts_v2") {
            std::string pd = params.value("prompts_dir", std::string());
            return emit(aicad::agent::get_prompts_v2(pd), out_json, 0);
        }
        if (m == "agent_load_cheatsheet") {
            std::string ft = params.value("feature_type", std::string());
            std::string pd = params.value("prompts_dir", std::string());
            return emit(json{{"text", aicad::agent::load_cheatsheet(ft, pd)}}, out_json, 0);
        }
        // --- P53a: orchestrator state machine + state/context (agent/orchestrator) ---
        if (m == "agent_est_tokens") {
            std::string text = params.value("text", std::string());
            return emit(json{{"n", aicad::agent::est_tokens(text)}}, out_json, 0);
        }
        if (m == "agent_err_sig") {
            std::string err = params.value("err", std::string());
            return emit(json{{"sig", aicad::agent::err_sig(err)}}, out_json, 0);
        }
        if (m == "agent_digest_tree") {
            // Re-parse from the RAW request text as ordered_json so object key
            // iteration preserves insertion order (matching Python dict order).
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson tree = op.contains("tree") ? op["tree"] : ojson::object();
            return emit(json{{"digest", aicad::agent::digest_tree(tree)}}, out_json, 0);
        }
        if (m == "agent_digest_params") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson p = op.contains("params") ? op["params"] : ojson::object();
            return emit(json{{"s", aicad::agent::digest_params(p)}}, out_json, 0);
        }
        if (m == "agent_looks_like_edit") {
            std::string req = params.value("request", std::string());
            return emit(json{{"edit", aicad::agent::looks_like_edit(req)}}, out_json, 0);
        }
        if (m == "agent_feature_status_errors") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson fs = op.contains("feature_status") ? op["feature_status"] : ojson::array();
            return emit(json{{"errors", aicad::agent::feature_status_errors(fs)}}, out_json, 0);
        }
        if (m == "agent_build_context") {
            // {state:{...}, role, prompts_dir?} -> {system, body, context_chars,
            // context_tokens_est}. Re-parse as ordered_json for spec/plan/memory.
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson st = op.contains("state") ? op["state"] : ojson::object();
            std::string role = op.value("role", std::string());
            std::string pd = op.value("prompts_dir", std::string());
            aicad::agent::DesignState ds;
            ds.request = st.value("request", std::string());
            ds.doc_id = st.value("doc_id", std::string());
            ds.revision = st.value("revision", 0);
            ds.spec = st.contains("spec") ? st["spec"] : ojson(nullptr);
            ds.plan = st.contains("plan") ? st["plan"] : ojson(nullptr);
            ds.tree_digest = st.value("tree_digest", std::string());
            if (st.contains("last_errors") && st["last_errors"].is_array())
                for (const auto& e : st["last_errors"]) ds.last_errors.push_back(e.get<std::string>());
            if (st.contains("last_issues") && st["last_issues"].is_array())
                for (const auto& e : st["last_issues"]) ds.last_issues.push_back(e.get<std::string>());
            ds.memory = (st.contains("memory") && st["memory"].is_object()) ? st["memory"]
                                                                            : ojson::object();
            ds.current_step = st.value("current_step", std::string());
            aicad::agent::BuiltContext bc = aicad::agent::build_context(ds, role, pd);
            return emit(json{{"system", bc.system},
                             {"body", bc.body},
                             {"context_chars", bc.context_chars},
                             {"context_tokens_est", bc.context_tokens_est}},
                        out_json, 0);
        }
        if (m == "agent_run_machine") {
            // SCRIPTED machine drive: {script:{intake:[...],...}, plan_steps:[...],
            // budgets?:{}, cancel_after?:int} -> {status, stop_reason, events:[...]}.
            json script = params.contains("script") ? params["script"] : json::object();
            json plan_steps_j = params.contains("plan_steps") ? params["plan_steps"] : json::array();
            json budgets = params.contains("budgets") ? params["budgets"] : json::object();
            bool has_cancel = params.contains("cancel_after") &&
                              params["cancel_after"].is_number_integer();
            int cancel_after = has_cancel ? params["cancel_after"].get<int>() : -1;

            // Per-stage queues of scripted outcomes (popped in call order).
            auto build_queue = [&](const char* stage) {
                std::vector<aicad::agent::StageOutcome> q;
                if (script.contains(stage) && script[stage].is_array())
                    for (const auto& o : script[stage]) {
                        aicad::agent::StageOutcome so;
                        so.verdict = o.value("verdict", std::string("pass"));
                        so.reason = o.value("reason", std::string());
                        if (o.contains("error") && !o["error"].is_null()) {
                            so.has_error = true;
                            so.error = o["error"].get<std::string>();
                        }
                        so.done = o.value("done", false);
                        so.raw = o.value("raw", std::string());
                        so.parsed_ok = o.value("parsed_ok", true);
                        if (o.contains("parse_error") && !o["parse_error"].is_null()) {
                            so.has_parse_error = true;
                            so.parse_error = o["parse_error"].get<std::string>();
                        }
                        if (o.contains("ops_count") && !o["ops_count"].is_null()) {
                            so.has_ops_count = true;
                            so.ops_count = o["ops_count"].get<int>();
                        }
                        so.duration_ms = o.value("duration_ms", 0);
                        so.no_progress = o.value("no_progress", false);
                        q.push_back(so);
                    }
                return q;
            };

            auto intake_q = std::make_shared<std::vector<aicad::agent::StageOutcome>>(
                build_queue("intake"));
            auto plan_q =
                std::make_shared<std::vector<aicad::agent::StageOutcome>>(build_queue("plan"));
            auto model_q =
                std::make_shared<std::vector<aicad::agent::StageOutcome>>(build_queue("model"));
            auto review_q =
                std::make_shared<std::vector<aicad::agent::StageOutcome>>(build_queue("review"));
            auto fix_q =
                std::make_shared<std::vector<aicad::agent::StageOutcome>>(build_queue("fix"));
            auto memory_q =
                std::make_shared<std::vector<aicad::agent::StageOutcome>>(build_queue("memory"));

            // Invocation counter for cancel_after (counts action calls = agent calls).
            auto invoked = std::make_shared<int>(0);

            auto make_action = [&](std::shared_ptr<std::vector<aicad::agent::StageOutcome>> q) {
                auto cursor = std::make_shared<size_t>(0);
                return [q, cursor, invoked](aicad::agent::DesignState& s,
                                            int) -> aicad::agent::StageOutcome {
                    (*invoked)++;
                    aicad::agent::StageOutcome o;
                    if (*cursor < q->size()) {
                        o = (*q)[*cursor];
                        (*cursor)++;
                    } else if (!q->empty()) {
                        o = q->back();  // repeat last (defensive; scripts should supply enough)
                    } else {
                        o.verdict = "pass";
                    }
                    s.last_no_progress = o.no_progress;
                    return o;
                };
            };

            aicad::agent::StageActions actions;
            actions.intake = make_action(intake_q);
            actions.plan = make_action(plan_q);
            actions.model = make_action(model_q);
            actions.review = make_action(review_q);
            actions.fix = make_action(fix_q);
            actions.memory = make_action(memory_q);
            std::vector<std::string> steps;
            if (plan_steps_j.is_array())
                for (const auto& s : plan_steps_j) steps.push_back(s.get<std::string>());
            actions.plan_steps = [steps](aicad::agent::DesignState&) { return steps; };

            aicad::agent::RunControl control;
            if (has_cancel) {
                control.cancelled = [invoked, cancel_after] {
                    return *invoked >= cancel_after;
                };
            }

            // RunEvents to a temp run dir AND capture every emitted event.
            std::string run_id = "gate-" + std::to_string(std::time(nullptr)) + "-" +
                                 std::to_string(reinterpret_cast<uintptr_t>(&actions));
            // Capture wrapper: subclass-free — we read back the jsonl after the run,
            // but to capture WITHOUT volatile run_id we just collect emitted events.
            aicad::agent::RunEvents events(run_id);
            // Drive the machine, then read the persisted events back (the source of
            // truth; identical to what emit() returned).
            aicad::agent::DesignState ds;
            ds.request = params.value("request", std::string("test request"));
            json result = aicad::agent::run_machine(ds, actions, events, budgets, control);

            // Read back captured events from the jsonl.
            json captured = json::array();
            {
                std::filesystem::path p = events.dir() / "events.jsonl";
                std::ifstream fh(p, std::ios::binary);
                std::string line;
                while (std::getline(fh, line)) {
                    if (!line.empty() && line.back() == '\r') line.pop_back();
                    if (line.empty()) continue;
                    captured.push_back(json::parse(line));
                }
            }
            result["events"] = captured;
            return emit(std::move(result), out_json, 0);
        }
        if (m == "agent_run") {
            // HOST-ERGONOMIC entry: {request, doc_id, options?, provider?}.
            // The host sends just {request, doc_id}; the core resolves everything:
            //   1) provider — if params.provider is absent/empty, resolve from the
            //      ambient config.json (load_config) via resolve_provider, honoring
            //      offline mode (resolve_provider returns {error} on a refusal). An
            //      explicit provider descriptor (the gates' mock) is passed through.
            //   2) micro-vs-design — should_engage(resolved-provider-cfg, cfg) picks
            //      run_micro_design vs run_design.
            // Returns the RunResult + events:[...], OR {ok:false, error} on a provider
            // refusal/resolution failure (no run started).
            std::string request = params.value("request", std::string());
            std::string doc_id = params.value("doc_id", std::string());
            json options = params.contains("options") ? params["options"] : json::object();

            // ---- provider resolution ----
            json provider;
            const bool explicit_provider = params.contains("provider") &&
                                           params["provider"].is_object() &&
                                           !params["provider"].empty();
            json cfg = aicad::agent::load_config();
            if (explicit_provider) {
                provider = params["provider"];  // pass-through (gate mock / host override)
            } else {
                // Resolve from config.json over the live process env (the same inputs
                // the Python agent uses). resolve_provider applies the offline guard.
                json resolved = aicad::agent::resolve_provider(std::nullopt, cfg, json::object());
                if (resolved.is_object() && resolved.contains("error")) {
                    return emit(json{{"ok", false},
                                     {"error", resolved.value("error", std::string("provider resolution failed"))}},
                                out_json, 0);
                }
                provider = resolved;
            }

            // ---- micro vs design ----
            // should_engage reads provider.{name,base_url,context_tokens} + cfg
            // .{micro_mode,context_tokens}. The resolved descriptor carries name/
            // base_url; cfg is the ambient config.json (so micro_mode is honored).
            bool micro = aicad::agent::should_engage(provider, cfg);

            auto captured = std::make_shared<json>(json::array());
            json result;
            if (micro) {
                aicad::agent::EmitFnMicro cb = [captured](const json& ev) {
                    captured->push_back(ev);
                };
                result = aicad::agent::run_micro_design(request, doc_id, provider, cb, options);
            } else {
                aicad::agent::EmitFn cb = [captured](const json& ev) { captured->push_back(ev); };
                result = aicad::agent::run_design(request, doc_id, provider, cb, options);
            }
            result["events"] = *captured;
            result["engaged"] = micro ? "micro" : "design";  // diagnostic: which profile ran
            return emit(std::move(result), out_json, 0);
        }
        if (m == "agent_run_design") {
            // LIVE end-to-end run: {request, doc_id, provider:{...}, options?} ->
            // {run_id, status, stop_reason, revision, doc_id, events:[...]}. The
            // provider descriptor is the resolved {name,model,api_key,...} OR the
            // role-scripted mock {name:"mock", replies:{by_role}} for the gate.
            std::string request = params.value("request", std::string());
            std::string doc_id = params.value("doc_id", std::string());
            json provider = params.contains("provider") ? params["provider"] : json::object();
            json options = params.contains("options") ? params["options"] : json::object();
            // Capture every emitted event (like agent_run_machine returns `events`).
            auto captured = std::make_shared<json>(json::array());
            aicad::agent::EmitFn cb = [captured](const json& ev) { captured->push_back(ev); };
            json result = aicad::agent::run_design(request, doc_id, provider, cb, options);
            result["events"] = *captured;
            return emit(std::move(result), out_json, 0);
        }
        if (m == "agent_run_micro") {
            // MICRO-ORCHESTRATION run: {request, doc_id, provider:{...}, options?} ->
            // {run_id, status, stop_reason, revision, doc_id, events:[...]}. Parallels
            // agent_run_design (P54a).
            std::string request = params.value("request", std::string());
            std::string doc_id = params.value("doc_id", std::string());
            json provider = params.contains("provider") ? params["provider"] : json::object();
            json options = params.contains("options") ? params["options"] : json::object();
            auto captured = std::make_shared<json>(json::array());
            aicad::agent::EmitFnMicro cb = [captured](const json& ev) {
                captured->push_back(ev);
            };
            json result =
                aicad::agent::run_micro_design(request, doc_id, provider, cb, options);
            result["events"] = *captured;
            return emit(std::move(result), out_json, 0);
        }
        if (m == "agent_should_engage") {
            // Pure: {provider:{name,base_url?,context_tokens?}, cfg:{micro_mode?,
            // context_tokens?}} -> {engage:bool}.
            json provider = params.contains("provider") ? params["provider"] : json::object();
            json cfg = params.contains("cfg") ? params["cfg"] : json::object();
            return emit(json{{"engage", aicad::agent::should_engage(provider, cfg)}}, out_json, 0);
        }
        if (m == "agent_micro_normalize_steps") {
            // Pure: {raw:[...]} -> {steps:[{type,desc,params}, ...]}.
            aicad::agent::ojson raw = params.contains("raw")
                                          ? aicad::agent::ojson::parse(params["raw"].dump())
                                          : aicad::agent::ojson(nullptr);
            std::vector<aicad::agent::Step> steps = aicad::agent::normalize_steps(raw);
            json out = json::array();
            for (const auto& s : steps)
                out.push_back(json{{"type", s.type}, {"desc", s.desc},
                                   {"params", json::parse(s.params.dump())}});
            return emit(json{{"steps", out}}, out_json, 0);
        }
        if (m == "agent_micro_summarize_doc") {
            // Pure: {tree, volume?} -> {summary}. volume present => "volume=N" head.
            aicad::agent::ojson tree =
                params.contains("tree") ? aicad::agent::ojson::parse(params["tree"].dump())
                                        : aicad::agent::ojson::object();
            std::optional<double> vol;
            if (params.contains("volume") && params["volume"].is_number())
                vol = params["volume"].get<double>();
            return emit(json{{"summary", aicad::agent::summarize_doc(tree, vol)}}, out_json, 0);
        }
        if (m == "agent_micro_verify_step") {
            // Pure: {step:{type,desc,params}, tree, feature_status, vol_before?, vol_after?}
            // -> {verified, notes}.
            aicad::agent::Step step;
            if (params.contains("step") && params["step"].is_object()) {
                const json& sp = params["step"];
                step.type = sp.value("type", std::string());
                step.desc = sp.value("desc", std::string());
                if (sp.contains("params") && sp["params"].is_object())
                    step.params = aicad::agent::ojson::parse(sp["params"].dump());
            }
            aicad::agent::ojson tree =
                params.contains("tree") ? aicad::agent::ojson::parse(params["tree"].dump())
                                        : aicad::agent::ojson::object();
            aicad::agent::ojson fs =
                params.contains("feature_status")
                    ? aicad::agent::ojson::parse(params["feature_status"].dump())
                    : aicad::agent::ojson::array();
            std::optional<double> vb, va;
            if (params.contains("vol_before") && params["vol_before"].is_number())
                vb = params["vol_before"].get<double>();
            if (params.contains("vol_after") && params["vol_after"].is_number())
                va = params["vol_after"].get<double>();
            aicad::agent::VerifyResult vr = aicad::agent::verify_step(step, tree, fs, vb, va);
            return emit(json{{"verified", vr.verified}, {"notes", vr.notes}}, out_json, 0);
        }
        // --- P54b: in-app LLM assists (agent/ask.py + agent/pm_assist.py) ---
        // All re-parse params_json as ordered_json so doc/form_schema/current
        // key order survives end-to-end (matching Python dict insertion order).
        if (m == "agent_build_doc_context") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson doc = op.contains("doc") ? op["doc"] : ojson(nullptr);
            std::string doc_id = op.value("doc_id", std::string("?"));
            long rev = op.contains("revision") && op["revision"].is_number()
                           ? op["revision"].get<long>()
                           : 0;
            ojson fs = op.contains("feature_status") ? op["feature_status"] : ojson::array();
            return emit(json{{"text", aicad::agent::build_doc_context(doc, doc_id, rev, fs)}},
                        out_json, 0);
        }
        if (m == "agent_build_ask_prompt") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            std::string question = op.value("question", std::string());
            ojson doc = op.contains("doc") ? op["doc"] : ojson(nullptr);
            std::string doc_id = op.value("doc_id", std::string("?"));
            long rev = op.contains("revision") && op["revision"].is_number()
                           ? op["revision"].get<long>()
                           : 0;
            ojson fs = op.contains("feature_status") ? op["feature_status"] : ojson::array();
            aicad::agent::AskPrompt ap =
                aicad::agent::build_ask_prompt(question, doc, doc_id, rev, fs);
            return emit(json{{"system", ap.system}, {"body", ap.body}}, out_json, 0);
        }
        if (m == "agent_answer_question") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            std::string question = op.value("question", std::string());
            std::string doc_id = op.value("doc_id", std::string("?"));
            // P54b host-ergonomics: hydrate doc/revision/feature_status from the live
            // C++ session when not passed explicitly (host sends just {question, doc_id}).
            hydrate_assist_from_session(op, doc_id);
            ojson doc = op.contains("doc") ? op["doc"] : ojson(nullptr);
            long rev = op.contains("revision") && op["revision"].is_number()
                           ? op["revision"].get<long>()
                           : 0;
            ojson fs = op.contains("feature_status") ? op["feature_status"] : ojson::array();
            // Provider: explicit descriptor (gate mock) OR resolved from config.json when
            // the host omits it. On an offline refusal, answer with a clear note.
            json prov = resolve_assist_provider(params);
            if (prov.is_object() && prov.contains("error")) {
                return emit(json{{"answer", std::string("I can't answer that right now: ") +
                                                prov.value("error", std::string("provider unavailable"))}},
                            out_json, 0);
            }
            auto provider = aicad::agent::make_assist_provider(prov);
            std::string answer = aicad::agent::answer_question(
                question, doc, doc_id, rev, fs,
                [&](const std::string& sys, const std::string& body) {
                    std::string err;
                    return provider(sys, body, &err);  // ask path ignores err
                });
            return emit(json{{"answer", answer}}, out_json, 0);
        }
        if (m == "agent_pm_extract_json") {
            std::string reply = params.value("reply", std::string());
            aicad::agent::ojson plan = aicad::agent::pm_extract_json(reply);
            return emit(json{{"plan", json::parse(plan.dump())}}, out_json, 0);
        }
        if (m == "agent_pm_coerce") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson field = op.contains("field") ? op["field"] : ojson::object();
            ojson value = op.contains("value") ? op["value"] : ojson(nullptr);
            ojson v = aicad::agent::pm_coerce_patch_value(field, value);
            return emit(json{{"value", json::parse(v.dump())}}, out_json, 0);
        }
        if (m == "agent_pm_validate_plan") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson plan = op.contains("plan") ? op["plan"] : ojson::object();
            ojson fs = op.contains("form_schema") ? op["form_schema"] : ojson::object();
            ojson cur = op.contains("current") ? op["current"] : ojson::object();
            ojson v = aicad::agent::pm_validate_plan(plan, fs, cur);
            return emit(json{{"validated", json::parse(v.dump())}}, out_json, 0);
        }
        if (m == "agent_pm_heuristic_plan") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson fs = op.contains("form_schema") ? op["form_schema"] : ojson::object();
            ojson cur = op.contains("current") ? op["current"] : ojson::object();
            std::string intent = op.value("intent", std::string());
            ojson v = aicad::agent::pm_heuristic_plan(fs, cur, intent);
            return emit(json{{"plan", json::parse(v.dump())}}, out_json, 0);
        }
        if (m == "agent_pm_build_prompt") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson prm = op.contains("params") ? op["params"] : ojson::object();
            ojson doc = op.contains("doc") ? op["doc"] : ojson(nullptr);
            std::string doc_id = op.value("doc_id", std::string("?"));
            long rev = op.contains("revision") && op["revision"].is_number()
                           ? op["revision"].get<long>()
                           : 0;
            ojson fs = op.contains("feature_status") ? op["feature_status"] : ojson::array();
            aicad::agent::PmAssistPrompt pp =
                aicad::agent::build_pm_assist_prompt(prm, doc, doc_id, rev, fs);
            return emit(json{{"system", pp.system}, {"body", pp.body}}, out_json, 0);
        }
        if (m == "agent_pm_assist") {
            using ojson = nlohmann::ordered_json;
            ojson op = ojson::parse(params_json && params_json[0] ? params_json : "{}");
            ojson prm = op.contains("params") ? op["params"] : ojson::object();
            std::string doc_id = op.value("doc_id", std::string("?"));
            // P54b host-ergonomics: hydrate doc/revision/feature_status from the live
            // C++ session when not passed explicitly (host sends just {params, doc_id}).
            hydrate_assist_from_session(op, doc_id);
            ojson doc = op.contains("doc") ? op["doc"] : ojson(nullptr);
            long rev = op.contains("revision") && op["revision"].is_number()
                           ? op["revision"].get<long>()
                           : 0;
            ojson fs = op.contains("feature_status") ? op["feature_status"] : ojson::array();
            // Provider: explicit descriptor (gate path) OR resolved from config.json when
            // the host omits it. An EXPLICIT descriptor is always honored as-is (the gate's
            // {error:"boom"} mock signals a provider that raises — NOT a config refusal). On
            // the config-fallback path, a refusal/resolution failure ({error}) or an empty
            // descriptor leaves cb=nullptr so assist falls back to its heuristic plan.
            bool has_explicit = params.contains("provider") && !params["provider"].is_null();
            aicad::agent::PmCompleteFn cb = nullptr;
            json prov = has_explicit ? params["provider"] : resolve_assist_provider(params);
            bool usable = has_explicit
                              ? !prov.is_null()
                              : (prov.is_object() && !prov.contains("error") && !prov.empty());
            if (usable) {
                auto provider = aicad::agent::make_assist_provider(prov);
                cb = [provider](const std::string& sys, const std::string& body,
                                std::string* err) { return provider(sys, body, err); };
            }
            ojson v = aicad::agent::assist_property_manager(prm, doc, doc_id, rev, fs, cb);
            return emit(json{{"validated", json::parse(v.dump())}}, out_json, 0);
        }
        if (m == "sim_fea_static") {
            // Linear-static FEA (engine/sim/fea_static.py parity probe). Params carry an
            // ALREADY-RESOLVED problem: points[n*3], tets[m*4], E_mpa, nu, yield_mpa,
            // fixed_nodes[], nodal_forces[n*3]. Returns displacement/von_mises/summary.
            auto getd = [&](const char* k) {
                std::vector<double> v;
                if (params.contains(k) && params[k].is_array())
                    for (const auto& x : params[k]) v.push_back(x.get<double>());
                return v;
            };
            auto geti = [&](const char* k) {
                std::vector<int> v;
                if (params.contains(k) && params[k].is_array())
                    for (const auto& x : params[k]) v.push_back(x.get<int>());
                return v;
            };
            aicad::sim::FeaStaticResult r = aicad::sim::fea_static_solve(
                getd("points"), geti("tets"), params.value("E_mpa", 0.0),
                params.value("nu", 0.0), params.value("yield_mpa", 250.0),
                geti("fixed_nodes"), getd("nodal_forces"));
            if (!r.ok) return emit(json{{"ok", false}, {"error", r.error}}, out_json, 0);
            return emit(json{{"ok", true},
                             {"displacement", r.displacement},
                             {"disp_magnitude", r.disp_magnitude},
                             {"von_mises", r.von_mises},
                             {"n_dof", r.n_dof},
                             {"summary",
                              {{"max_von_mises_mpa", r.max_von_mises_mpa},
                               {"max_disp_mm", r.max_disp_mm},
                               {"min_fos", r.min_fos}}}},
                        out_json, 0);
        }
        if (m == "sim_fea_modal") {
            // Modal FEA (engine/sim/fea_modal.py parity probe). Params: points[n*3],
            // tets[m*4], E_mpa, nu, density_g_cm3, fixed_nodes[], n_modes. Returns
            // freqs_hz[] + mode_shapes[n_modes*n*3].
            auto getd = [&](const char* k) {
                std::vector<double> v;
                if (params.contains(k) && params[k].is_array())
                    for (const auto& x : params[k]) v.push_back(x.get<double>());
                return v;
            };
            auto geti = [&](const char* k) {
                std::vector<int> v;
                if (params.contains(k) && params[k].is_array())
                    for (const auto& x : params[k]) v.push_back(x.get<int>());
                return v;
            };
            aicad::sim::FeaModalResult r = aicad::sim::fea_modal_solve(
                getd("points"), geti("tets"), params.value("E_mpa", 0.0),
                params.value("nu", 0.0), params.value("density_g_cm3", 0.0),
                geti("fixed_nodes"), params.value("n_modes", 6));
            if (!r.ok) return emit(json{{"ok", false}, {"error", r.error}}, out_json, 0);
            return emit(json{{"ok", true},
                             {"freqs_hz", r.freqs_hz},
                             {"mode_shapes", r.mode_shapes},
                             {"n_modes", r.n_modes},
                             {"n_nodes", r.n_nodes},
                             {"n_dof", r.n_dof}},
                        out_json, 0);
        }
        if (m == "sim_spice") {
            // Native MNA circuit sim (engine/sim/circuit.py parity probe; linear DC for
            // now). Params: {circuit:{...}, study:{...}}. Returns the sim.md result shape.
            const json& circ = params.contains("circuit") ? params["circuit"] : json::object();
            const json& study = params.contains("study") ? params["study"] : json::object();
            return emit(aicad::sim::spice_simulate(circ, study), out_json, 0);
        }
        if (m == "sim_numpy_normal") {
            // DIAGNOSTIC: bit-exact-parity probe for the numpy RNG that drives the Monte-Carlo
            // (mc) SPICE analysis. Params: {"seed": int, "n": int}. Returns
            // {"draws": [...]} == np.random.default_rng(seed).standard_normal(size=n).
            long long seed = params.contains("seed") && params["seed"].is_number()
                                 ? params["seed"].get<long long>() : 0;
            int n = params.contains("n") && params["n"].is_number()
                        ? params["n"].get<int>() : 0;
            aicad::sim::NumpyGenerator rng(static_cast<uint64_t>(seed));
            json draws = json::array();
            for (int i = 0; i < n; ++i) draws.push_back(rng.standard_normal());
            return emit(json{{"draws", draws}}, out_json, 0);
        }
        if (m == "sim_netlist_parse") {
            // DIAGNOSTIC: parse a SPICE netlist deck text into the Circuit dict
            // (engine/sim/spice.py:parse_spice + subckt.py parity probe). Params:
            // {"netlist": "<deck text>"}. Returns the frozen Circuit dict (sim.md §1)
            // so the host's .cir flow can run on the C++ core (it then feeds sim_spice).
            std::string deck = params.contains("netlist") && params["netlist"].is_string()
                                   ? params["netlist"].get<std::string>()
                                   : std::string();
            return emit(aicad::sim::parse_netlist(deck), out_json, 0);
        }
        if (m == "sim_netlist_to_circuit") {
            // DIAGNOSTIC: map a schematic netlist dict (engine/ecad/netlist.py
            // build_netlist output) to a (Circuit, warnings) pair — the schematic->SPICE
            // translation ecad_simulate drives (engine/sim/spice.py:netlist_to_spice_circuit
            // parity probe). Params: {"netlist": {...}, "sim_params"?}. Returns
            // {"circuit": {...}, "warnings": [...]}.
            const json& nl = params.contains("netlist") ? params["netlist"] : json::object();
            const json& sp = params.contains("sim_params") ? params["sim_params"] : json(nullptr);
            return emit(aicad::sim::netlist_to_spice_circuit(nl, sp), out_json, 0);
        }
        if (m == "sim_lbm") {
            // D3Q19 BGK LBM wind-tunnel kernel (engine/sim/lbm.py parity probe). The
            // trimesh voxelization is NOT done here: the caller passes an ALREADY-
            // precomputed solid mask + lattice params + step count so this isolates the
            // lattice numerics (the eventual CUDA payload). Contract:
            //   mask : {"nx","ny","nz","solid":[flat (Nx,Ny,Nz) row-major 0/1 ints]}
            //          solid[(x*Ny+y)*Nz+z] != 0 => solid cell.
            //   params : {"u_lat","tau0","voxel_mm","dx_m","dt_s","rho_air"}
            //   n_steps : number of LBM time steps.
            // Returns: {"ok", nx,ny,nz, ux,uy,uz,rho (flat (Nx,Ny,Nz) row-major),
            //           cd, drag_n, lift_n, frontal_area_mm2, steps_run, diverged}.
            const json& mask = params.contains("mask") ? params["mask"] : json::object();
            const json& pp = params.contains("params") ? params["params"] : json::object();
            int nx = mask.value("nx", 0);
            int ny = mask.value("ny", 0);
            int nz = mask.value("nz", 0);
            std::vector<uint8_t> solid;
            if (mask.contains("solid") && mask["solid"].is_array()) {
                solid.reserve(mask["solid"].size());
                for (const auto& v : mask["solid"]) solid.push_back(v.get<int>() ? 1 : 0);
            }
            aicad::sim::LbmParams lp;
            lp.u_lat = pp.value("u_lat", 0.05);
            lp.tau0 = pp.value("tau0", 1.0);
            lp.voxel_mm = pp.value("voxel_mm", 1.0);
            lp.dx_m = pp.value("dx_m", 1.0e-3);
            lp.dt_s = pp.value("dt_s", 1.0e-6);
            lp.rho_air = pp.value("rho_air", 1.225);
            int n_steps = params.value("n_steps", 0);
            aicad::sim::LbmResult r = aicad::sim::lbm_run(solid, nx, ny, nz, lp, n_steps);
            if (!r.ok) return emit(json{{"ok", false}, {"error", r.error}}, out_json, 0);
            return emit(json{{"ok", true},
                             {"nx", r.nx},
                             {"ny", r.ny},
                             {"nz", r.nz},
                             {"ux", r.ux},
                             {"uy", r.uy},
                             {"uz", r.uz},
                             {"rho", r.rho},
                             {"cd", r.cd},
                             {"drag_n", r.drag_n},
                             {"lift_n", r.lift_n},
                             {"frontal_area_mm2", r.frontal_area_mm2},
                             {"steps_run", r.steps_run},
                             {"diverged", r.diverged}},
                        out_json, 0);
        }
        if (m == "sim_mesh") {
            // gmsh tet4 mesher (engine/sim/mesher.py parity probe). Wraps the SAME
            // libgmsh 4.15.2 as the Python oracle, so the mesh is bit-identical.
            // Params: {"step_path": "<path>", "density": <float, default 0.5>}.
            // Returns points[n*3], tets[m*4] (0-based), char_len_mm, n_nodes, n_elems.
            std::string step_path = params.contains("step_path") && params["step_path"].is_string()
                                        ? params["step_path"].get<std::string>()
                                        : std::string();
            if (step_path.empty())
                return emit_error("sim_mesh: 'step_path' (string) is required", out_json);
            double density = params.value("density", 0.5);
            aicad::sim::MeshOut r = aicad::sim::mesh_step(step_path, density);
            if (!r.ok) return emit(json{{"ok", false}, {"error", r.error}}, out_json, 0);
            return emit(json{{"ok", true},
                             {"points", r.points},
                             {"tets", r.tets},
                             {"char_len_mm", r.char_len_mm},
                             {"n_nodes", r.n_nodes},
                             {"n_elems", r.n_elems}},
                        out_json, 0);
        }
        if (m == "sim_run_static") {
            // P50b: combined mesh + BC-resolve + linear-static solve (the glue on
            // top of sim_mesh + sim_fea_static). Params: {"study": {...}} where the
            // study is the host's shape — step_path|points/tets, density, material,
            // fixtures:[{points,tol_mm}], loads:[{faces,type,vector|magnitude}].
            // Returns {ok, displacement, disp_magnitude, von_mises, n_dof, summary,
            // solver_notes}. (overlay/npz are display-only and out of scope here.)
            const json& study = params.contains("study") ? params["study"] : json::object();
            json res = aicad::sim::fea_run_static(study);
            // P55b: when the study carries a STEP path, ALSO package a display overlay
            // (best-effort — mirrors the Python worker _static_overlay_path; an overlay
            // failure NEVER fails the solve). Existing result fields are untouched.
            if (res.value("ok", false) && study.contains("step_path") &&
                study["step_path"].is_string()) {
                const std::string step_path = study["step_path"].get<std::string>();
                if (std::filesystem::exists(step_path) && res.contains("von_mises")) {
                    std::vector<double> vol = flat_doubles(study.value("points", json::array()));
                    if (vol.empty()) {
                        // Inline points absent: re-mesh to recover the volume node coords
                        // (the solve already meshed; this just re-derives the cloud — the
                        // mesh is deterministic so node coords match the solved result).
                        double density = study.value("density", 0.5);
                        aicad::sim::MeshOut mo = aicad::sim::mesh_step(step_path, density);
                        if (mo.ok) vol = mo.points;
                    }
                    std::vector<double> vm = flat_doubles(res.value("von_mises", json::array()));
                    std::vector<double> dm = flat_doubles(res.value("disp_magnitude", json::array()));
                    std::vector<double> dv = flat_doubles(res.value("displacement", json::array()));
                    double yield_mpa = 250.0;
                    if (study.contains("material") && study["material"].is_object())
                        yield_mpa = study["material"].value("yield_mpa", 250.0);
                    json summary = res.contains("summary") ? res["summary"] : json::object();
                    std::string ov =
                        aicad::sim::static_overlay(step_path, vol, vm, dm, dv, yield_mpa, summary);
                    if (!ov.empty()) res["overlay_aicm"] = ov;
                }
            }
            return emit(std::move(res), out_json, 0);
        }
        if (m == "sim_run_modal") {
            // P50b: combined mesh + fixture-resolve + modal solve. Params:
            // {"study": {...}, "n_modes": <int, default 6>}. Returns {ok, freqs_hz,
            // mode_shapes, n_modes, n_nodes, n_dof}.
            const json& study = params.contains("study") ? params["study"] : json::object();
            int n_modes = params.value("n_modes", 6);
            json res = aicad::sim::fea_run_modal(study, n_modes);
            // P55b: when the study carries a STEP path, ALSO package a display overlay
            // for mode 0 (best-effort — mirrors the Python worker _modal_overlay_path;
            // an overlay failure NEVER fails the solve). Existing fields are untouched.
            if (res.value("ok", false) && study.contains("step_path") &&
                study["step_path"].is_string()) {
                const std::string step_path = study["step_path"].get<std::string>();
                if (std::filesystem::exists(step_path) && res.contains("mode_shapes") &&
                    res["mode_shapes"].is_array() && !res["mode_shapes"].empty() &&
                    res.contains("freqs_hz") && res["freqs_hz"].is_array() &&
                    !res["freqs_hz"].empty()) {
                    std::vector<double> vol = flat_doubles(study.value("points", json::array()));
                    if (vol.empty() && study.contains("step_path")) {
                        // Inline points absent: re-mesh to get the volume node coords.
                        double density = study.value("density", 0.5);
                        aicad::sim::MeshOut mo = aicad::sim::mesh_step(step_path, density);
                        if (mo.ok) vol = mo.points;
                    }
                    std::vector<double> mode0 = flat_doubles(res["mode_shapes"][0]);
                    double freq = res["freqs_hz"][0].get<double>();
                    std::string ov = aicad::sim::modal_overlay(step_path, vol, mode0, freq);
                    if (!ov.empty()) res["overlay_aicm"] = ov;
                }
            }
            return emit(std::move(res), out_json, 0);
        }
        if (m == "aicm_scalar_chunk") {
            // DIAGNOSTIC (byte-contract proof): return the SCLR chunk bytes (hex) for a
            // crafted (body_id, name, values, vmin, vmax) so the gate can byte-compare
            // against engine.doc.tessellate.scalar_chunk.
            uint32_t body_id = params.value("body_id", 1u);
            std::string name = params.value("name", std::string());
            std::vector<double> vals_d = flat_doubles(params.value("values", json::array()));
            std::vector<float> vals(vals_d.begin(), vals_d.end());
            float vmin = static_cast<float>(params.value("vmin", 0.0));
            float vmax = static_cast<float>(params.value("vmax", 0.0));
            std::vector<uint8_t> bytes = aicm::scalar_chunk(body_id, name, vals, vmin, vmax);
            return emit(json{{"hex", to_hex(bytes)}}, out_json, 0);
        }
        if (m == "aicm_disp_chunk") {
            // DIAGNOSTIC: return the DISP chunk bytes (hex) for crafted (body_id,
            // scale_hint, vectors) — byte-compares against tessellate.disp_chunk.
            uint32_t body_id = params.value("body_id", 1u);
            float scale_hint = static_cast<float>(params.value("scale_hint", 1.0));
            std::vector<double> vec_d = flat_doubles(params.value("vectors", json::array()));
            std::vector<float> vec(vec_d.begin(), vec_d.end());
            std::vector<uint8_t> bytes = aicm::disp_chunk(body_id, scale_hint, vec);
            return emit(json{{"hex", to_hex(bytes)}}, out_json, 0);
        }
        if (m == "sim_map_to_surface") {
            // DIAGNOSTIC (map value-parity): nearest-volume-node mapping. Accepts
            // {surf:[[x,y,z]...], vol:[[x,y,z]...], values:[...]| [[...]...], dim?}.
            // dim is inferred from values shape when omitted (3 if nested triples, else 1).
            std::vector<double> surf = flat_doubles(params.value("surf", json::array()));
            std::vector<double> vol = flat_doubles(params.value("vol", json::array()));
            const json& vj = params.value("values", json::array());
            int dim = 1;
            if (params.contains("dim") && params["dim"].is_number()) {
                dim = params["dim"].get<int>();
            } else if (vj.is_array() && !vj.empty() && vj[0].is_array()) {
                dim = static_cast<int>(vj[0].size());
            }
            std::vector<double> values = flat_doubles(vj);
            std::vector<double> mapped = aicad::sim::map_to_surface(surf, vol, values, dim);
            return emit(json{{"mapped", mapped}, {"dim", dim}}, out_json, 0);
        }
        if (m == "sim_static_overlay") {
            // P55b: package a static-result overlay AICM from a STEP + volume results.
            // {step_path, points[n*3], von_mises[n], disp_magnitude[n],
            //  displacement[n*3], yield_mpa?, summary?} -> {overlay_aicm}.
            const std::string step_path = params.value("step_path", std::string());
            std::vector<double> pts = flat_doubles(params.value("points", json::array()));
            std::vector<double> vm = flat_doubles(params.value("von_mises", json::array()));
            std::vector<double> dm = flat_doubles(params.value("disp_magnitude", json::array()));
            std::vector<double> dv = flat_doubles(params.value("displacement", json::array()));
            double yield_mpa = params.value("yield_mpa", 250.0);
            json summary = params.contains("summary") ? params["summary"] : json::object();
            std::string ov =
                aicad::sim::static_overlay(step_path, pts, vm, dm, dv, yield_mpa, summary);
            if (ov.empty()) return emit_error("sim_static_overlay: overlay packaging failed", out_json);
            return emit(json{{"overlay_aicm", ov}}, out_json, 0);
        }
        if (m == "sim_modal_overlay") {
            // P55b: package a single-mode modal overlay AICM.
            // {step_path, points[n*3], mode_shape[n*3], freq_hz?} -> {overlay_aicm}.
            const std::string step_path = params.value("step_path", std::string());
            std::vector<double> pts = flat_doubles(params.value("points", json::array()));
            std::vector<double> shp = flat_doubles(params.value("mode_shape", json::array()));
            double freq_hz = params.value("freq_hz", 0.0);
            std::string ov = aicad::sim::modal_overlay(step_path, pts, shp, freq_hz);
            if (ov.empty()) return emit_error("sim_modal_overlay: overlay packaging failed", out_json);
            return emit(json{{"overlay_aicm", ov}}, out_json, 0);
        }
        if (m == "doc_validate") {
            // DIAGNOSTIC: structural-validate a document tree (schema.validate_document
            // parity probe). Accepts {"doc":{...}} or a top-level {"features":[...]}.
            json doc;
            if (params.contains("doc")) doc = params["doc"];
            else if (params.contains("features")) doc = params;
            else return emit_error("doc_validate: params need a 'doc' object", out_json);
            std::vector<std::string> errs = aicad::doc::validate_document(doc);
            return emit(json{{"ok", errs.empty()}, {"errors", errs}}, out_json, 0);
        }
        // --- stateful document session RPCs (aicad::doc::session) ---------------
        // Each returns the host's ops-response JSON. An OpError becomes a nonzero
        // return carrying the {code,message,detail} envelope (so the host can treat
        // it as a structured error / fall back to Python).
        try {
            if (m == "doc_new")
                return emit(aicad::doc::doc_new(params.value("kind", std::string("part")),
                                                params.value("name", std::string())),
                            out_json, 0);
            if (m == "doc_apply_ops")
                return emit(aicad::doc::doc_apply_ops(
                                params.value("doc_id", std::string()),
                                params.contains("base_revision") && params["base_revision"].is_number()
                                    ? params["base_revision"].get<long>() : -1,
                                params.contains("ops") ? params["ops"] : json::array(),
                                params.value("coalesce", false)),
                            out_json, 0);
            if (m == "doc_dry_run_ops")
                return emit(aicad::doc::doc_dry_run_ops(
                                params.value("doc_id", std::string()),
                                params.contains("ops") ? params["ops"] : json::array()),
                            out_json, 0);
            if (m == "doc_undo")
                return emit(aicad::doc::doc_undo(params.value("doc_id", std::string())), out_json, 0);
            if (m == "doc_redo")
                return emit(aicad::doc::doc_redo(params.value("doc_id", std::string())), out_json, 0);
            if (m == "doc_tree")
                return emit(aicad::doc::doc_tree(params.value("doc_id", std::string())), out_json, 0);
            if (m == "doc_rebuild")
                return emit(aicad::doc::doc_rebuild(params.value("doc_id", std::string())), out_json, 0);
            if (m == "doc_close")
                return emit(json{{"closed", aicad::doc::doc_close(params.value("doc_id", std::string()))}},
                            out_json, 0);
            if (m == "doc_list")
                return emit(aicad::doc::doc_list(), out_json, 0);
            if (m == "doc_checkpoint")
                return emit(aicad::doc::doc_checkpoint(params.value("doc_id", std::string())), out_json, 0);
            if (m == "doc_revert")
                return emit(aicad::doc::doc_revert(params.value("doc_id", std::string()),
                                                   params.value("checkpoint", std::string())),
                            out_json, 0);
            if (m == "config_list")
                return emit(aicad::doc::doc_config_list(params.value("doc_id", std::string())),
                            out_json, 0);
            if (m == "config_add")
                return emit(aicad::doc::doc_config_add(
                                params.value("doc_id", std::string()),
                                params.value("name", std::string()),
                                params.contains("copy_from") ? params["copy_from"] : json(nullptr)),
                            out_json, 0);
            if (m == "config_delete")
                return emit(aicad::doc::doc_config_delete(params.value("doc_id", std::string()),
                                                          params.value("name", std::string())),
                            out_json, 0);
            if (m == "config_activate")
                return emit(aicad::doc::doc_config_activate(params.value("doc_id", std::string()),
                                                            params.value("name", std::string())),
                            out_json, 0);
            if (m == "config_set_override")
                return emit(aicad::doc::doc_config_set_override(
                                params.value("doc_id", std::string()),
                                params.value("name", std::string()),
                                params.value("feature_id", std::string()),
                                params.value("param", std::string()),
                                params.contains("value") ? params["value"] : json(nullptr),
                                params.value("clear", false)),
                            out_json, 0);
            if (m == "config_set_suppression")
                return emit(aicad::doc::doc_config_set_suppression(
                                params.value("doc_id", std::string()),
                                params.value("name", std::string()),
                                params.value("feature_id", std::string()),
                                params.value("suppressed", false)),
                            out_json, 0);
            if (m == "doc_save")
                return emit(aicad::doc::doc_save(params.value("doc_id", std::string()),
                                                 params.value("path", std::string())),
                            out_json, 0);
            if (m == "doc_open")
                return emit(aicad::doc::doc_open(params.value("path", std::string())), out_json, 0);
            if (m == "doc_resolve_pick")
                return emit(aicad::doc::doc_resolve_pick(
                                params.value("doc_id", std::string()),
                                params.value("body_id", 0),
                                params.value("kind", std::string()),
                                params.value("topo_id", 0)),
                            out_json, 0);
            if (m == "doc_push_pull")
                return emit(aicad::doc::doc_push_pull(
                                params.value("doc_id", std::string()),
                                params.value("body_id", 0),
                                params.value("topo_id", 0),
                                params.contains("distance") && params["distance"].is_number()
                                    ? params["distance"].get<double>() : 0.0,
                                params.value("commit", false)),
                            out_json, 0);
            if (m == "doc_export_step")
                return emit(aicad::doc::doc_export_step(
                                params.value("doc_id", std::string()),
                                params.value("path", std::string())),
                            out_json, 0);
            if (m == "doc_face_points")
                return emit(aicad::doc::doc_face_points(
                                params.value("doc_id", std::string()),
                                params.contains("selector") ? params["selector"] : json(nullptr),
                                params.contains("max_n") && params["max_n"].is_number()
                                    ? params["max_n"].get<int>() : 400),
                            out_json, 0);
            // --- C18 interactive sketch mode (engine/doc/session.py sketch_*) ---
            // The worker passes the sketch feature id under "sketch".
            if (m == "sketch_enter")
                return emit(aicad::doc::doc_sketch_enter(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string())),
                            out_json, 0);
            if (m == "sketch_solve")
                return emit(aicad::doc::doc_sketch_solve(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string())),
                            out_json, 0);
            if (m == "doc_sketch_overview")
                return emit(aicad::doc::doc_sketch_overview(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string())),
                            out_json, 0);
            if (m == "doc_sketch_regions")
                return emit(aicad::doc::doc_sketch_regions(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string())),
                            out_json, 0);
            if (m == "sketch_drag")
                return emit(aicad::doc::doc_sketch_drag(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string()),
                                params.value("handle", std::string()),
                                params.contains("xy") ? params["xy"] : json(nullptr),
                                params.contains("targets") ? params["targets"] : json(nullptr)),
                            out_json, 0);
            if (m == "sketch_drag_commit")
                return emit(aicad::doc::doc_sketch_drag_commit(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string())),
                            out_json, 0);
            if (m == "sketch_exit")
                return emit(aicad::doc::doc_sketch_exit(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string())),
                            out_json, 0);
            if (m == "sketch_probe")
                return emit(aicad::doc::doc_sketch_probe(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string()),
                                params.contains("action") ? params["action"] : json::object()),
                            out_json, 0);
            if (m == "sketch_project_ref")
                return emit(aicad::doc::doc_sketch_project_ref(
                                params.value("doc_id", std::string()),
                                params.value("sketch", std::string()),
                                params.contains("ref") ? params["ref"] : json(nullptr)),
                            out_json, 0);
            if (m == "eval_mass")
                return emit(aicad::doc::doc_eval_mass(
                                params.value("doc_id", std::string()),
                                params.contains("body") && params["body"].is_number()
                                    ? params["body"].get<int>() : 0,
                                params.contains("density_g_cm3") && params["density_g_cm3"].is_number()
                                    ? params["density_g_cm3"].get<double>() : 7.85),
                            out_json, 0);
            if (m == "eval_check")
                return emit(aicad::doc::doc_eval_check(
                                params.value("doc_id", std::string()),
                                params.contains("body") && params["body"].is_number()
                                    ? params["body"].get<int>() : 0),
                            out_json, 0);
            if (m == "eval_section_props")
                return emit(aicad::doc::doc_eval_section_props(
                                params.value("doc_id", std::string()),
                                params.contains("plane") ? params["plane"] : json(nullptr)),
                            out_json, 0);
            if (m == "eval_symmetry")
                return emit(aicad::doc::doc_eval_symmetry(
                                params.value("doc_id", std::string()),
                                params.contains("body") && params["body"].is_number()
                                    ? params["body"].get<int>() : 0,
                                params.contains("plane") ? params["plane"] : json(nullptr)),
                            out_json, 0);
            if (m == "eval_interference")
                return emit(aicad::doc::doc_eval_interference(
                                params.value("doc_id", std::string())),
                            out_json, 0);
            if (m == "eval_measure")
                return emit(aicad::doc::doc_eval_measure(
                                params.value("doc_id", std::string()),
                                params.contains("refs") ? params["refs"] : json::array()),
                            out_json, 0);
            if (m == "eval_draft") {
                std::array<double, 3> pull{0.0, 0.0, 1.0};
                if (params.contains("pull") && params["pull"].is_array() &&
                    params["pull"].size() == 3) {
                    for (int i = 0; i < 3; ++i) pull[i] = params["pull"][i].get<double>();
                }
                return emit(aicad::doc::doc_eval_draft(
                                params.value("doc_id", std::string()),
                                params.contains("body") && params["body"].is_number()
                                    ? params["body"].get<int>() : 0,
                                pull,
                                params.contains("angle_deg") && params["angle_deg"].is_number()
                                    ? params["angle_deg"].get<double>() : 0.0),
                            out_json, 0);
            }
            if (m == "eval_curvature")
                return emit(aicad::doc::doc_eval_curvature(
                                params.value("doc_id", std::string()),
                                params.contains("body") && params["body"].is_number()
                                    ? params["body"].get<int>() : 0,
                                params.value("kind", std::string("mean"))),
                            out_json, 0);
            if (m == "eval_thickness")
                return emit(aicad::doc::doc_eval_thickness(
                                params.value("doc_id", std::string()),
                                params.contains("body") && params["body"].is_number()
                                    ? params["body"].get<int>() : 0),
                            out_json, 0);
            if (m == "export_file")
                return emit(aicad::doc::doc_export_file(params.value("doc_id", std::string()),
                                                        params.value("path", std::string()),
                                                        params.value("format", std::string())),
                            out_json, 0);
            if (m == "import_file")
                return emit(aicad::doc::doc_import_file(params.value("path", std::string()),
                                                        params.value("into", std::string()),
                                                        params.value("as_solid", false),
                                                        params.value("format", std::string())),
                            out_json, 0);
            if (m == "mesh_read_raw")
                return emit(aicad::doc::mesh_read_raw(params.value("path", std::string()),
                                                      params.value("format", std::string())),
                            out_json, 0);
            // --- 2D DRAWING projection + export (engine/doc/drawing.py + export2d.py) ---
            // STATELESS diagnostic form (the strangler parity probe): compile a part
            // tree + project its views via OCCT HLR, or pool the views to DXF/PDF.
            if (m == "doc_drawing_views") {
                json doc;
                if (params.contains("doc")) doc = params["doc"];
                else if (params.contains("features")) doc = params;
                else throw aicad::doc::OpError("invalid_op",
                                               "doc_drawing_views: params need a 'doc' object");
                json env = params.contains("env") ? params["env"] : json::object();
                json views = params.contains("views") ? params["views"] : json::array();
                return emit(aicad::doc::drawing_views(doc, env, views), out_json, 0);
            }
            if (m == "export2d_drawing") {
                json doc;
                if (params.contains("doc")) doc = params["doc"];
                else if (params.contains("features")) doc = params;
                else throw aicad::doc::OpError("invalid_op",
                                               "export2d_drawing: params need a 'doc' object");
                json env = params.contains("env") ? params["env"] : json::object();
                json views = params.contains("views") ? params["views"] : json::array();
                return emit(aicad::doc::export2d_drawing(doc, env, views,
                                                         params.value("path", std::string()),
                                                         params.value("format", std::string())),
                            out_json, 0);
            }
            // --- STATEFUL DrawingSession lifecycle (engine/doc/drawing.py) -----
            // A live drawing doc that projects a source part tree through the same
            // HLR path on every fetch (associative). Mirrors the doc_* / ecad_*
            // session registries: an OpError becomes a nonzero {code,message,detail}.
            auto opt = [&](const char* k) -> json {
                return params.contains(k) ? params[k] : json(nullptr);
            };
            if (m == "drawing_new") {
                json src;
                if (params.contains("source_doc")) src = params["source_doc"];
                else if (params.contains("doc")) src = params["doc"];
                else if (params.contains("features")) src = params;
                else throw aicad::doc::OpError("invalid_op",
                                               "drawing_new: params need a 'source_doc' (or 'doc')");
                json env = params.contains("env") ? params["env"] : json::object();
                return emit(aicad::doc::drawing_new(src, env, params.value("name", std::string())),
                            out_json, 0);
            }
            if (m == "drawing_set_source") {
                json src;
                if (params.contains("source_doc")) src = params["source_doc"];
                else if (params.contains("doc")) src = params["doc"];
                else throw aicad::doc::OpError("invalid_op",
                                               "drawing_set_source: params need a 'source_doc'");
                json env = params.contains("env") ? params["env"] : json::object();
                return emit(aicad::doc::drawing_set_source(params.value("doc_id", std::string()),
                                                           src, env),
                            out_json, 0);
            }
            if (m == "drawing_add_view")
                return emit(aicad::doc::drawing_add_view(
                                params.value("doc_id", std::string()),
                                params.value("type", std::string()),
                                params.value("orientation", std::string("front")),
                                params.value("sheet_id", std::string()),
                                opt("scale"), opt("position"), opt("show_hidden")),
                            out_json, 0);
            if (m == "drawing_add_section")
                return emit(aicad::doc::drawing_add_section(
                                params.value("doc_id", std::string()),
                                params.value("parent_view_id", std::string()),
                                opt("plane"), opt("cut_line"), opt("label"), opt("scale"),
                                opt("position"), params.value("sheet_id", std::string()),
                                opt("show_hidden")),
                            out_json, 0);
            if (m == "drawing_add_detail")
                return emit(aicad::doc::drawing_add_detail(
                                params.value("doc_id", std::string()),
                                params.value("parent_view_id", std::string()),
                                opt("center"),
                                params.contains("radius") && params["radius"].is_number()
                                    ? params["radius"].get<double>() : 0.0,
                                opt("scale"), opt("label"), opt("position"),
                                params.value("sheet_id", std::string()), opt("show_hidden")),
                            out_json, 0);
            if (m == "drawing_remove_view")
                return emit(aicad::doc::drawing_remove_view(params.value("doc_id", std::string()),
                                                            params.value("view_id", std::string())),
                            out_json, 0);
            if (m == "drawing_set_view")
                return emit(aicad::doc::drawing_set_view(
                                params.value("doc_id", std::string()),
                                params.value("view_id", std::string()),
                                opt("scale"), opt("position"), opt("orientation"),
                                opt("show_hidden")),
                            out_json, 0);
            if (m == "drawing_add_dim")
                return emit(aicad::doc::drawing_add_dim(
                                params.value("doc_id", std::string()),
                                params.value("view_id", std::string()),
                                params.value("type", std::string("linear_dim")),
                                opt("a"), opt("b"), opt("c"), opt("d"), opt("pos"), opt("text")),
                            out_json, 0);
            if (m == "drawing_remove_annotation")
                return emit(aicad::doc::drawing_remove_annotation(
                                params.value("doc_id", std::string()),
                                params.value("view_id", std::string()),
                                params.value("ann_id", std::string())),
                            out_json, 0);
            if (m == "drawing_get")
                return emit(aicad::doc::drawing_get(params.value("doc_id", std::string())),
                            out_json, 0);
            if (m == "drawing_close")
                return emit(json{{"closed", aicad::doc::drawing_close(
                                                params.value("doc_id", std::string()))}},
                            out_json, 0);
        } catch (const aicad::doc::OpError& oe) {
            // Structured error envelope (matches the Python worker's doc_* errors).
            return emit(oe.envelope(), out_json, 3);
        }
        // --- stateful ECAD document session RPCs (aicad::ecad::session) ----------
        // Same contract as the doc_* block: an OpError becomes a nonzero return
        // carrying the {code,message,detail} envelope. The ECAD doc/ops layer is
        // pure JSON (no OCCT) — schematic + board document model + apply-ops.
        try {
            if (m == "ecad_new")
                return emit(aicad::ecad::ecad_new(params.value("kind", std::string("sch")),
                                                  params.value("name", std::string())),
                            out_json, 0);
            if (m == "ecad_apply_ops")
                return emit(aicad::ecad::ecad_apply_ops(
                                params.value("doc_id", std::string()),
                                params.contains("base_revision") && params["base_revision"].is_number()
                                    ? params["base_revision"].get<long>() : -1,
                                params.contains("ops") ? params["ops"] : json::array()),
                            out_json, 0);
            if (m == "ecad_undo")
                return emit(aicad::ecad::ecad_undo(params.value("doc_id", std::string())), out_json, 0);
            if (m == "ecad_redo")
                return emit(aicad::ecad::ecad_redo(params.value("doc_id", std::string())), out_json, 0);
            if (m == "ecad_tree")
                return emit(aicad::ecad::ecad_tree(params.value("doc_id", std::string())), out_json, 0);
            if (m == "ecad_rebuild")
                return emit(aicad::ecad::ecad_rebuild(params.value("doc_id", std::string())), out_json, 0);
            if (m == "ecad_close")
                return emit(json{{"closed", aicad::ecad::ecad_close(params.value("doc_id", std::string()))}},
                            out_json, 0);
            if (m == "ecad_describe")
                return emit(aicad::ecad::ecad_describe(params.value("doc_id", std::string())),
                            out_json, 0);
            if (m == "ecad_schema")
                return emit(aicad::ecad::ecad_schema(params.value("kind", std::string("sch"))),
                            out_json, 0);
            if (m == "ecad_dfm") {
                json rules = params.contains("rules") ? params["rules"] : json(nullptr);
                if (params.contains("board") && params["board"].is_object())
                    return emit(aicad::ecad::ecad_dfm_board(params["board"], rules), out_json, 0);
                return emit(aicad::ecad::ecad_dfm(params.value("doc_id", std::string()), rules),
                            out_json, 0);
            }
            auto base_rev = [&](void) -> long {
                return params.contains("base_revision") && params["base_revision"].is_number()
                           ? params["base_revision"].get<long>()
                           : -1;
            };
            if (m == "ecad_autoroute") {
                json opts = params.contains("opts") ? params["opts"] : json(nullptr);
                if (params.contains("board") && params["board"].is_object())
                    // Stateless inline-board path (parity harness): returns the PURE
                    // {tracks, vias, report} of engine.ecad.autoroute.autoroute(board, opts).
                    return emit(aicad::ecad::autoroute(params["board"], opts), out_json, 0);
                // Doc-id session path (the host's form): route the open board, apply the
                // tracks/vias as ops, return the board ops-response + report.
                return emit(aicad::ecad::ecad_autoroute(params.value("doc_id", std::string()), opts,
                                                        base_rev()),
                            out_json, 0);
            }
            if (m == "ecad_route_diff_pair") {
                json opts = params.contains("opts") ? params["opts"] : json(nullptr);
                if (params.contains("board") && params["board"].is_object())
                    return emit(aicad::ecad::route_diff_pair(params["board"],
                                                             params.value("net_p", std::string()),
                                                             params.value("net_n", std::string()),
                                                             opts),
                                out_json, 0);
                return emit(aicad::ecad::ecad_route_diff_pair(
                                params.value("doc_id", std::string()),
                                params.value("net_p", std::string()),
                                params.value("net_n", std::string()), opts, base_rev()),
                            out_json, 0);
            }
            if (m == "ecad_fill_zones") {
                json resolution = params.contains("resolution") ? params["resolution"] : json(nullptr);
                return emit(aicad::ecad::ecad_fill_zones(params.value("doc_id", std::string()),
                                                         resolution, base_rev()),
                            out_json, 0);
            }
            if (m == "ecad_simulate") {
                json study = params.contains("study") ? params["study"] : json(nullptr);
                json sim_params = params.contains("sim_params") ? params["sim_params"] : json(nullptr);
                return emit(aicad::ecad::ecad_simulate(params.value("doc_id", std::string()), study,
                                                       sim_params),
                            out_json, 0);
            }
            if (m == "ecad_to_3d") {
                json opts = params.contains("opts") ? params["opts"] : json(nullptr);
                if (params.contains("board") && params["board"].is_object())
                    // Stateless inline-board path (parity harness): build the OCCT board
                    // model (substrate extrude + drill cuts + analytic component proxies)
                    // and return the geometry summary + exact BRep volume stats. Mirrors
                    // engine.ecad.threed.board_to_step's summary; no STEP file is written.
                    return emit(aicad::ecad::board_to_3d(params["board"], opts), out_json, 0);
                // Doc-id session path (the host's form): fetch the open board doc, build
                // the model, WRITE the STEP at `path`, and return {step_path, summary, ...}
                // exactly like engine.ecad.threed.board_to_step.
                json board = aicad::ecad::ecad_session_doc(params.value("doc_id", std::string()));
                if (!aicad::ecad::schema::is_board(board))
                    return emit(aicad::ecad::OpError("invalid_op",
                                                     "ecad_to_3d requires a pcb document",
                                                     json{{"reason", "wrong doc kind"}})
                                    .envelope(),
                                out_json, 3);
                return emit(aicad::ecad::board_to_3d_step(board, opts,
                                                          params.value("path", std::string())),
                            out_json, 0);
            }
            if (m == "ecad_impedance")
                return emit(aicad::ecad::ecad_impedance(params), out_json, 0);
            if (m == "ecad_export")
                return emit(aicad::ecad::ecad_export(params), out_json, 0);
            if (m == "ecad_save")
                return emit(aicad::ecad::ecad_save(params.value("doc_id", std::string()),
                                                   params.value("path", std::string())),
                            out_json, 0);
            if (m == "ecad_open")
                return emit(aicad::ecad::ecad_open(params.value("path", std::string())), out_json, 0);
            if (m == "ecad_update_from_sch") {
                // Stateless inline path (parity harness): {board, sch_netlist, prune?,
                // place_origin?} -> sync's {mutated_board, report}. Else the stateful
                // path over two open sessions.
                json place = params.contains("place_origin") ? params["place_origin"] : json(nullptr);
                if (params.contains("board") && params["board"].is_object())
                    return emit(aicad::ecad::ecad_update_from_sch_inline(
                                    params["board"],
                                    params.contains("sch_netlist") ? params["sch_netlist"]
                                                                    : json::object(),
                                    params.value("prune", false), place),
                                out_json, 0);
                return emit(aicad::ecad::ecad_update_from_sch(
                                params.value("board_doc_id", std::string()),
                                params.value("sch_doc_id", std::string()),
                                params.value("prune", false), place,
                                params.contains("base_revision") && params["base_revision"].is_number()
                                    ? params["base_revision"].get<long>() : -1),
                            out_json, 0);
            }
            // Deterministic recipe generators (engine/ecad/recipes.py). The LLM
            // orchestrator (engine/ecad/orchestrator.py / the ecad_design RPC) is
            // DEFERRED to the agent/LLM epic; only the pure recipe builders are ported.
            if (m == "ecad_recipe")
                return emit(aicad::ecad::recipes::build(
                                params.value("name", std::string()),
                                params.contains("params") ? params["params"] : json::object()),
                            out_json, 0);
            if (m == "ecad_recipes_all")
                return emit(aicad::ecad::recipes::all_recipes(), out_json, 0);
            if (m == "ecad_closest_recipe")
                return emit(aicad::ecad::recipes::closest_recipe(params.value("request", std::string())),
                            out_json, 0);
        } catch (const aicad::ecad::OpError& oe) {
            return emit(oe.envelope(), out_json, 3);
        }
        // Unknown to the C++ core -> nonzero so the host falls back to Python.
        return emit(json{{"error", "unknown method: " + m}}, out_json, 2);
    } catch (const std::exception& e) {
        return emit_error(std::string("aicad_call(") + m + ") threw: " + e.what(), out_json);
    } catch (...) {
        return emit_error("aicad_call(" + m + ") threw a non-std exception", out_json);
    }
}

}  // extern "C"
