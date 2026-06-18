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

#include "agent/config.h"
#include "agent/events.h"
#include "agent/machine.h"
#include "agent/paths.h"
#include "agent/ops_emit.h"
#include "agent/providers.h"
#include "agent/roles.h"
#include "agent/state.h"
#include "doc/document.h"
#include "doc/export2d.h"
#include "doc/session.h"
#include "doc/validate.h"
#include "ecad/autoroute.h"
#include "ecad/recipes.h"
#include "ecad/session.h"
#include "geom/ecad_3d.h"
#include "sim/fea_modal.h"
#include "sim/fea_run.h"
#include "sim/fea_static.h"
#include "sim/lbm.h"
#include "sim/mesher.h"
#include "sim/netlist.h"
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
            return emit(aicad::sim::fea_run_static(study), out_json, 0);
        }
        if (m == "sim_run_modal") {
            // P50b: combined mesh + fixture-resolve + modal solve. Params:
            // {"study": {...}, "n_modes": <int, default 6>}. Returns {ok, freqs_hz,
            // mode_shapes, n_modes, n_nodes, n_dof}.
            const json& study = params.contains("study") ? params["study"] : json::object();
            int n_modes = params.value("n_modes", 6);
            return emit(aicad::sim::fea_run_modal(study, n_modes), out_json, 0);
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
            if (m == "ecad_autoroute" && params.contains("board") && params["board"].is_object()) {
                // Stateless inline-board path (parity harness): returns the PURE
                // {tracks, vias, report} of engine.ecad.autoroute.autoroute(board, opts).
                json opts = params.contains("opts") ? params["opts"] : json(nullptr);
                return emit(aicad::ecad::autoroute(params["board"], opts), out_json, 0);
            }
            if (m == "ecad_route_diff_pair" && params.contains("board") &&
                params["board"].is_object()) {
                json opts = params.contains("opts") ? params["opts"] : json(nullptr);
                return emit(aicad::ecad::route_diff_pair(params["board"],
                                                         params.value("net_p", std::string()),
                                                         params.value("net_n", std::string()), opts),
                            out_json, 0);
            }
            if (m == "ecad_to_3d" && params.contains("board") && params["board"].is_object()) {
                // Stateless inline-board path (parity harness): build the OCCT board
                // model (substrate extrude + drill cuts + analytic component proxies)
                // and return the geometry summary + exact BRep volume stats. Mirrors
                // engine.ecad.threed.board_to_step's summary; the STEP-file side effect
                // is omitted (the host's stateful ecad_to_3d still routes to Python).
                json opts = params.contains("opts") ? params["opts"] : json(nullptr);
                return emit(aicad::ecad::board_to_3d(params["board"], opts), out_json, 0);
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
