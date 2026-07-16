// core/agent/run.cpp — see run.h. Mirrors agent/orchestrator/run.py (P53b).
#include "agent/run.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <optional>
#include <regex>
#include <set>
#include <sstream>

#include "agent/config.h"
#include "agent/events.h"
#include "agent/logging_provider.h"
#include "agent/machine.h"
#include "agent/mission.h"
#include "agent/ops_emit.h"
#include "agent/providers.h"
#include "agent/roles.h"
#include "agent/state.h"
#include "doc/session.h"

namespace aicad {
namespace agent {

namespace {

using ojson = nlohmann::ordered_json;

template<typename JsonT>
std::string jstr(const JsonT& j, const char* key, const std::string& d = std::string()) {
    auto it = j.find(key);
    if (it == j.end() || !it->is_string()) return d;
    return it->template get<std::string>();
}

// How many parse/validate failures before the modeler escape hatch (raw build123d script)
// fires. Kept deliberately high so the agent keeps trying REAL parametric ops — each retry is
// re-prompted with the concrete validation error (state.last_errors flows into build_context),
// which is how it self-corrects. The script escape is a genuine last resort, not a 2-strike
// default (the old value of 2 made the agent bail to a canned script on anything non-trivial).
constexpr int kEscapeAfter = 4;

// Per-call output-token budget by provider + reasoning effort. The hardcoded 4096 throttled
// capable cloud models (a complex part is one modeler call → truncation → parse fail → escape
// hatch). Capable cloud backends get real headroom; local models stay modest.
int call_budget_for(const json& provider_cfg) {
    // Explicit user override (config "max_output_tokens" -> provider.max_tokens) wins. This is the
    // knob the AI settings expose: raise it when a reasoning model spends its output budget
    // thinking and returns no/truncated JSON. Clamp to a sane floor so a tiny value can't starve
    // every stage; no hard ceiling (the user opts into the cost).
    if (provider_cfg.contains("max_tokens") && provider_cfg["max_tokens"].is_number_integer()) {
        int mt = provider_cfg["max_tokens"].get<int>();
        if (mt > 0) return mt < 512 ? 512 : mt;
    }
    std::string name = provider_cfg.value("name", std::string());
    std::string effort = provider_cfg.value("reasoning_effort", std::string());
    int base = 4096;  // local / unknown
    if (name == "anthropic" || name == "anthropic-sub" || name == "gemini" || name == "openai" ||
        name == "xai" || name == "xai-sub")
        base = 8192;
    if (effort == "high" || effort == "max") base += 4096;  // reasoning headroom before content
    return base;
}

// Python truthiness of a JSON value (bool(x)): None/false/0/0.0/""/[]/{} -> false.
bool truthy_json(const ojson& v) {
    if (v.is_null()) return false;
    if (v.is_boolean()) return v.get<bool>();
    if (v.is_string()) return !v.get<std::string>().empty();
    if (v.is_number_integer()) return v.get<long long>() != 0;
    if (v.is_number_unsigned()) return v.get<unsigned long long>() != 0;
    if (v.is_number_float()) return v.get<double>() != 0.0;
    if (v.is_array() || v.is_object()) return !v.empty();
    return false;
}

// Python str(value) for a scalar/JSON used in an f-string (plan_steps dims/features).
// Mirrors json scalar -> Python repr in a "{v}" context: ints/floats use Python's repr,
// strings use the raw text, bools -> True/False, null -> None, containers -> json-ish.
std::string py_scalar_str(const ojson& v) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_boolean()) return v.get<bool>() ? "True" : "False";
    if (v.is_null()) return "None";
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    if (v.is_number_unsigned()) return std::to_string(v.get<unsigned long long>());
    if (v.is_number_float()) {
        double d = v.get<double>();
        // Python str(float): integral floats render as "N.0".
        std::ostringstream ss;
        if (d == static_cast<long long>(d)) {
            ss << static_cast<long long>(d) << ".0";
        } else {
            ss.precision(17);
            ss << d;
        }
        return ss.str();
    }
    // Containers: defensive — the gate's plan parts use int/string dims only.
    return v.dump();
}

// ----------------------------------------------------------------- json helpers

// _extract_json_obj(reply): first fenced ```json object, else a bare top-level
// {...} parsed to an object, else null. Returns ojson (preserves key order so a
// spec/plan flows into build_context byte-faithfully).
ojson extract_json_obj(const std::string& reply) {
    // Each fenced ```json (or untagged) block: try to parse as an object.
    std::regex fence(R"(```(?:json|JSON)?\s*\n([\s\S]*?)```)");
    auto begin = std::sregex_iterator(reply.begin(), reply.end(), fence);
    auto end = std::sregex_iterator();
    for (auto it = begin; it != end; ++it) {
        std::string body = (*it)[1].str();
        // strip()
        size_t a = body.find_first_not_of(" \t\r\n");
        size_t b = body.find_last_not_of(" \t\r\n");
        if (a == std::string::npos) continue;
        std::string trimmed = body.substr(a, b - a + 1);
        ojson d = ojson::parse(trimmed, nullptr, false);
        if (!d.is_discarded() && d.is_object()) return d;
    }
    // Bare first-balanced {..}.
    std::optional<std::string> blob = first_balanced(reply, '{', '}');
    if (blob) {
        ojson d = ojson::parse(*blob, nullptr, false);
        if (!d.is_discarded() && d.is_object()) return d;
    }
    return ojson(nullptr);
}

// _inputs_digest(state) = "rev{revision} feats={len(tree_digest.splitlines())}".
std::string inputs_digest(const DesignState& state) {
    // Python str.splitlines(): an empty string -> 0 lines; otherwise count line
    // terminators + a trailing nonterminated segment.
    int lines = 0;
    const std::string& d = state.tree_digest;
    if (!d.empty()) {
        lines = 1;
        for (size_t i = 0; i < d.size(); ++i) {
            if (d[i] == '\n') {
                // a trailing terminator does not add an extra empty segment.
                if (i + 1 < d.size()) lines += 1;
            }
        }
    }
    return "rev" + std::to_string(state.revision) + " feats=" + std::to_string(lines);
}

// _default_step(state): "Build the requested object: {spec.object or request}".
std::string default_step(const DesignState& state) {
    std::string obj;
    if (state.spec.is_object() && state.spec.contains("object") && state.spec["object"].is_string())
        obj = state.spec["object"].get<std::string>();
    if (obj.empty()) obj = state.request;
    return "Build the requested object: " + obj;
}

// ----------------------------------------------------------------- memory persistence

// _store_memory: write to run_dir(run_id)/memory.json (the session-attr/extra-entry
// embed is a save-time host concern — we store to disk + keep on the in-memory state).
void store_memory(const std::string& run_id, const ojson& mem) {
    std::filesystem::path p = run_dir(run_id) / "memory.json";
    std::ofstream fh(p, std::ios::binary | std::ios::trunc);
    if (fh) fh << mem.dump(2);
}

// ----------------------------------------------------------------- provider call

// A reply + duration + meta, exactly like run.py _agent_call returns.
struct AgentCall {
    std::string reply;
    std::string thinking;
    std::string model;
    int duration_ms = 0;
    std::string prompt_file;
    std::string prompt_hash;
    int context_chars = 0;
    int context_tokens_est = 0;
    std::string inputs_digest;
    bool has_call_error = false;
    std::string call_error;
};

// A reply + error from one provider completion (mirrors complete_ex).
struct ProvReply {
    std::string text;
    std::string thinking;
    std::string model;
    bool has_error = false;
    std::string error;
};

// One PERSISTENT provider instance for the whole run (the RoleScriptedProvider's
// per-role queues must survive across calls so the modeler's 1st/2nd/3rd replies are
// popped in order — exactly like the Python provider object lives for the run).
using ProviderFn = std::function<ProvReply(const std::string& system, const json& messages,
                                           int max_tokens)>;

ProviderFn make_provider(const json& provider_cfg) {
    std::string name = provider_cfg.value("name", std::string());
    if (name == "mock" && provider_cfg.contains("replies") &&
        provider_cfg["replies"].is_object()) {
        // Role-scripted mock (the gate path): sniff role from the system prompt and pop
        // the next scripted reply for that role. ONE instance, shared across all calls.
        auto rp = std::make_shared<RoleScriptedProvider>(provider_cfg["replies"]);
        return [rp](const std::string& system, const json&, int) -> ProvReply {
            ProvReply r;
            r.text = rp->complete(system);
            return r;
        };
    }
    // Real provider (or flat-list mock): route through agent::complete each call.
    json cfg = provider_cfg;
    return [name, cfg](const std::string& system, const json& messages, int max_tokens) -> ProvReply {
        ProvReply r;
        CompleteResult cr = complete(name, cfg, system, messages, max_tokens);
        r.thinking = cr.thinking;
        r.model = !cr.model.empty() ? cr.model : cfg.value("model", std::string());
        if (!cr.error.empty()) {
            r.has_error = true;
            r.error = cr.error;
            r.text = cr.text;
            return r;
        }
        r.text = cr.text;
        return r;
    };
}

// _agent_call(provider, state, role): build a FRESH bounded context, prepend the
// "ROLE: <role>" marker line, call the provider, capture meta. max_tokens = 2048.
AgentCall agent_call(const ProviderFn& provider, const DesignState& state, const std::string& role,
                     const std::string& prompts_dir, int max_tokens, bool vision,
                     int context_cap = kContextTokenCap,
                     const std::string& extra_directive = std::string()) {
    BuiltContext bc = build_context(state, role, prompts_dir, context_cap);
    std::string system = "ROLE: " + role + "\n" + bc.system;
    auto pr = load_prompt(role, prompts_dir);
    AgentCall ac;
    ac.prompt_file = pr.second;
    ac.prompt_hash = prompt_hash(system);
    ac.context_chars = bc.context_chars;
    ac.context_tokens_est = bc.context_tokens_est;
    ac.inputs_digest = inputs_digest(state);

    // Experimental: an optional run-scoped directive appended to the user body (empty by default,
    // so the context + the parity gate are unchanged). Used by cad_sequence_mode to nudge the
    // modeler toward an editable parametric feature sequence.
    std::string body = extra_directive.empty() ? bc.body : (bc.body + "\n\n" + extra_directive);

    // Vision: when the provider supports images and a render of the current model exists,
    // attach it so the model can SEE the geometry (reviewer/fixer self-correction).
    json parts = json::array({text_part(body)});
    if (vision && state.last_render.is_string() && !state.last_render.get<std::string>().empty())
        parts.push_back(image_part(state.last_render.get<std::string>()));
    json messages = json::array({user_message(parts)});

    auto t0 = std::chrono::steady_clock::now();
    ProvReply rep = provider(system, messages, max_tokens);  // provider/effort-scaled budget
    auto t1 = std::chrono::steady_clock::now();
    ac.duration_ms =
        static_cast<int>(std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
    ac.thinking = rep.thinking;
    ac.model = rep.model;
    if (rep.has_error) {
        ac.reply = "";
        ac.has_call_error = true;
        ac.call_error = rep.error;
    } else {
        ac.reply = rep.text;
    }
    return ac;
}

// Convert a doc-session response (regular json) tree into the ojson update_tree wants.
ojson tree_to_ojson(const json& tree) {
    // dump+parse preserves the doc layer's key order (sorted std::map) into ojson.
    return ojson::parse(tree.dump());
}

// feature_status_errors over a (regular json) feature_status list.
std::vector<std::string> status_errors(const json& feature_status) {
    ojson fs = ojson::parse(feature_status.dump());
    return feature_status_errors(fs);
}

}  // namespace

json run_design(const std::string& request, const std::string& doc_id, const json& provider_cfg,
                EmitFn emit, const json& options) {
    // run_id = options.run_id or uuid hex[:12].
    std::string run_id = options.value("run_id", std::string());
    if (run_id.empty()) {
        // A process-unique token (the gate always passes a fixed run_id, so the exact
        // shape here is never compared).
        run_id = "run-" + std::to_string(static_cast<unsigned long long>(
                              std::chrono::steady_clock::now().time_since_epoch().count()));
    }
    std::string prompts_dir = options.value("prompts_dir", std::string());

    // OFFLINE MODE defense-in-depth (assert_offline_safe). The host owns the app
    // config; the gate's mock is offline-safe and offline mode is off. We honor an
    // explicit options.offline flag + the provider descriptor's safety so the refusal
    // path is preserved without a config-file loader (DEFERRED: app-config read = host).
    bool offline = options.value("offline", false);
    if (offline && !provider_offline_safe(provider_cfg)) {
        // Mirror run.py: assert_offline_safe raises before any provider call. We surface
        // it as a failed run (the host translates to OfflineModeError).
        RunEvents ev_guard(run_id, emit);
        ev_guard.emit("run_finished", json{{"status", "failed"},
                                            {"stop_reason", "offline_mode_refused"},
                                            {"revision", 0}});
        return json{{"run_id", run_id},
                    {"status", "failed"},
                    {"stop_reason", "offline_mode_refused"},
                    {"revision", 0},
                    {"doc_id", doc_id}};
    }

    RunEvents events(run_id, emit);
    // Live provider_call debug events (endpoint/status/latency) land on the same
    // emit stream as stage events so the host can orch_-prefix them into chat.
    set_provider_call_emitter([&events](const json& ev) {
        json data = ev;
        std::string kind = "provider_call";
        if (data.contains("kind") && data["kind"].is_string()) {
            kind = data["kind"].get<std::string>();
            data.erase("kind");
        }
        events.emit(kind, data);
    });
    struct ProviderEmitGuard {
        ~ProviderEmitGuard() { set_provider_call_emitter(nullptr); }
    } provider_emit_guard;

    // ONE provider instance for the whole run (per-role scripted queues must persist).
    ProviderFn provider = make_provider(provider_cfg);

    // T-H7: resolve consent early (same logic as post-run; consent_tier/load_config are
    // cheap and idempotent — we re-read at emit time too, but need it now for the wrapper).
    std::string run_consent = consent_tier(load_config());
    if (offline) run_consent = "off";  // offline hard-overrides to "off" (same as emit site)

    // Wrap with timing + sidecar logging (thin; never changes behaviour).
    // LoggingProviderState collects the most-recent sidecar paths so emit_ai_artifact
    // (below) can pass input_ref/output_ref instead of empty strings.
    aicad::agent::LoggingProviderState lp_state;
    provider = aicad::agent::wrap_logging(std::move(provider), run_id, run_consent, &lp_state);
    // Output-token budget + INPUT context cap, jointly fitted to the model's context window so a
    // small local window is never overrun (which would truncate the prompt and break the chain).
    const CallBudget budget = fit_call_budget(provider_cfg, call_budget_for(provider_cfg));
    const int call_budget = budget.output;
    const int ctx_cap = budget.input_cap;
    // Whether the provider can receive screenshots (drives vision review/fix).
    const bool vision = provider_cfg.value("supports_vision", false);

    // Experimental: a SEPARATE provider for the vision review + fix stages (Seek-CAD split — a
    // strong/cloud planner emits ops while a local vision judge sees the render and self-corrects;
    // also enables an all-local ITAR review loop). Absent -> identical to the main provider, so the
    // behavior and the Python-parity gate are unchanged.
    ProviderFn review_provider = provider;
    bool review_vision = vision;
    int review_budget = call_budget;
    int review_ctx_cap = ctx_cap;
    bool review_provider_specialized = false;
    if (provider_cfg.contains("review_provider") && provider_cfg["review_provider"].is_object()) {
        const json& rpc = provider_cfg["review_provider"];
        review_provider = make_provider(rpc);
        review_vision = rpc.value("supports_vision", false);
        const CallBudget rb = fit_call_budget(rpc, call_budget_for(rpc));
        review_budget = rb.output;
        review_ctx_cap = rb.input_cap;
        review_provider_specialized = true;
        events.emit("run_note",
                    json{{"note", "experimental: separate vision-review provider"},
                         {"review_provider", rpc.value("name", std::string())},
                         {"review_model", rpc.value("model", std::string())}});
    }

    // Experimental: modeler prompt augmentation toward an editable parametric feature SEQUENCE
    // (DeepCAD/CAD-Coder-style design history) rather than a monolithic solid. Empty when off.
    const std::string cad_seq_directive =
        provider_cfg.value("cad_sequence_mode", false)
            ? std::string(
                  "[experimental: parametric sequence mode] Build the part as a minimal, ORDERED "
                  "sequence of individually editable features (sketch -> extrude/revolve/loft/sweep, "
                  "then fillet/chamfer/hole/pattern/mirror) that reconstructs a clean design history. "
                  "Avoid a single fused/imported solid; each feature must stay separately editable.")
            : std::string();

    // ---- PREWALK (strong plan → cheap implement) ----
    // When agent.prewalk is true and the target resolves, model/fix stages use a
    // separate cheap provider. Planning stages (intake/plan) keep the strong main
    // provider. One-way: first implementation stage emits a run_note and stays on
    // the cheap model for the rest of the run. Null target = safe no-op (identical
    // to prewalk off).
    ProviderFn implement_provider = provider;
    int implement_budget = call_budget;
    int implement_ctx_cap = ctx_cap;
    bool implement_vision = vision;
    bool prewalk_armed = false;
    bool prewalk_switched = false;
    std::string prewalk_model_label;
    {
        // options.prewalk / options.prewalk_target override ambient config (tests + host).
        json cfg_pw = load_config();
        if (!cfg_pw.is_object()) cfg_pw = json::object();
        if (!cfg_pw.contains("agent") || !cfg_pw["agent"].is_object())
            cfg_pw["agent"] = json::object();
        if (options.contains("prewalk")) cfg_pw["agent"]["prewalk"] = options["prewalk"];
        if (options.contains("prewalk_target") && options["prewalk_target"].is_string())
            cfg_pw["agent"]["prewalk_target"] = options["prewalk_target"].get<std::string>();
        // Also honor a pre-resolved descriptor the host/gate may attach.
        json pw_desc = json(nullptr);
        if (provider_cfg.contains("prewalk_provider") &&
            provider_cfg["prewalk_provider"].is_object() &&
            !provider_cfg["prewalk_provider"].contains("error")) {
            pw_desc = provider_cfg["prewalk_provider"];
        } else {
            pw_desc = resolve_prewalk_provider(cfg_pw, json::object());
        }
        if (pw_desc.is_object() && !pw_desc.contains("error") && !pw_desc.empty()) {
            // Same provider+model as main → nothing to switch (disarm silently).
            const std::string main_name = provider_cfg.value("name", std::string());
            const std::string main_model =
                provider_cfg.contains("model") && provider_cfg["model"].is_string()
                    ? provider_cfg["model"].get<std::string>()
                    : std::string();
            const std::string pw_name = pw_desc.value("name", std::string());
            const std::string pw_model = pw_desc.contains("model") && pw_desc["model"].is_string()
                                            ? pw_desc["model"].get<std::string>()
                                            : std::string();
            if (!(pw_name == main_name && pw_model == main_model && !pw_model.empty())) {
                implement_provider = make_provider(pw_desc);
                implement_provider =
                    aicad::agent::wrap_logging(std::move(implement_provider), run_id, run_consent,
                                               &lp_state);
                const CallBudget ib = fit_call_budget(pw_desc, call_budget_for(pw_desc));
                implement_budget = ib.output;
                implement_ctx_cap = ib.input_cap;
                implement_vision = pw_desc.value("supports_vision", false);
                prewalk_armed = true;
                prewalk_model_label =
                    pw_model.empty() ? pw_name : (pw_name.empty() ? pw_model : pw_name + "/" + pw_model);
            }
        }
    }
    // Lazy one-shot notice on first implementation-stage call.
    auto use_implement_provider = [&]() -> const ProviderFn& {
        if (prewalk_armed && !prewalk_switched) {
            prewalk_switched = true;
            events.emit("run_note",
                        json{{"note", "Prewalk: switched to " + prewalk_model_label +
                                          " for implementation"},
                             {"text", "Prewalk: switched to " + prewalk_model_label +
                                          " for implementation"},
                             {"prewalk", true},
                             {"model", prewalk_model_label}});
        }
        return prewalk_armed ? implement_provider : provider;
    };

    // Seed the DesignState from the live session: doc_tree(doc_id) gives {revision, tree}.
    json tree0 = doc::doc_tree(doc_id);
    DesignState state;
    state.request = request;
    state.doc_id = doc_id;
    state.revision = tree0.value("revision", 0);
    // _load_memory: the on-disk/session-embed load is a host concern; start empty.
    state.memory = ojson::object();
    state.update_tree(tree_to_ojson(tree0.contains("tree") ? tree0["tree"] : json::object()));

    json stages = json::array({"intake", "plan", "model", "review", "fix", "memory"});
    events.emit("run_started",
                json{{"request", request}, {"doc_id", state.doc_id}, {"stages", stages}});

    // ----------------------------------------------------------------- actions
    StageActions actions;

    // Shared helper: copy an AgentCall's meta into a StageOutcome.
    auto fill_meta = [](StageOutcome& o, const AgentCall& ac) {
        o.prompt_file = ac.prompt_file;
        o.prompt_hash = ac.prompt_hash;
        o.context_chars = ac.context_chars;
        o.context_tokens_est = ac.context_tokens_est;
        o.inputs_digest = ac.inputs_digest;
        o.duration_ms = ac.duration_ms;
        o.thinking = ac.thinking;
        o.model = ac.model;
    };

    // _apply(ops): doc_apply_ops at state.revision; update rev + tree; collect status errors.
    // On success (and on apply-with-status-errors) emit synthetic agent.tool_call Doing row.
    auto apply_ops = [&](DesignState& st, const json& ops) -> std::vector<std::string> {
        auto t0 = std::chrono::steady_clock::now();
        json resp = doc::doc_apply_ops(doc_id, st.revision, ops, /*coalesce=*/false);
        auto t1 = std::chrono::steady_clock::now();
        int lat = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
        st.update_revision(resp.value("revision", st.revision));
        st.update_tree(tree_to_ojson(resp.contains("tree") ? resp["tree"] : json::object()));
        json fs = json::array();
        if (resp.contains("rebuild") && resp["rebuild"].contains("feature_status"))
            fs = resp["rebuild"]["feature_status"];
        std::vector<std::string> errs = status_errors(fs);
        emit_agent_tool_call(events, /*tool_name=*/"", /*target=*/"", ops,
                             /*ok=*/errs.empty(), lat, /*parent_id=*/"", /*actor=*/"modeler");
        return errs;
    };

    // ---- INTAKE ----
    actions.intake = [&](DesignState& st, int) -> StageOutcome {
        AgentCall ac = agent_call(provider, st, "intake", prompts_dir, call_budget, vision, ctx_cap);
        ojson spec = extract_json_obj(ac.reply);
        StageOutcome o;
        fill_meta(o, ac);
        o.raw = ac.reply;
        if (ac.has_call_error) {
            o.verdict = "retry";
            o.reason = "provider error: " + ac.call_error;
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = ac.call_error;
            o.has_error = true;
            o.error = "intake: " + ac.call_error;
            return o;
        }
        if (!spec.is_null()) {
            st.spec = spec;
            o.verdict = "pass";
            o.reason = "spec parsed";
            o.parsed_ok = true;
        } else {
            o.verdict = "retry";
            o.reason = "spec not parseable";
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = "no spec json";
            o.has_error = true;
            o.error = "intake: no spec json";
        }
        return o;
    };

    // ---- PLAN ----
    actions.plan = [&](DesignState& st, int) -> StageOutcome {
        AgentCall ac = agent_call(provider, st, "planner", prompts_dir, call_budget, vision, ctx_cap);
        ojson p = extract_json_obj(ac.reply);
        StageOutcome o;
        fill_meta(o, ac);
        o.raw = ac.reply;
        if (ac.has_call_error) {
            o.verdict = "retry";
            o.reason = "provider error: " + ac.call_error;
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = ac.call_error;
            o.has_error = true;
            o.error = "plan: " + ac.call_error;
            return o;
        }
        bool ok = !p.is_null() && p.contains("parts") && p["parts"].is_array() &&
                  !p["parts"].empty();
        if (ok) {
            st.plan = p;
            o.verdict = "pass";
            o.reason = "plan parsed";
            o.parsed_ok = true;
        } else {
            // Plan is optional: a failure DEGRADES (retry -> exhaust -> non-fatal).
            o.verdict = "retry";
            o.reason = "plan not parseable";
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = "no plan json";
        }
        return o;
    };

    // ---- MODEL escape hatch ----
    auto model_escape = [&](DesignState& st) -> StageOutcome {
        // Append the escape instruction; then ask for a build123d script.
        std::string appended =
            st.current_step +
            "\n\nThe op attempts failed. Instead, output a build123d script (assign `result`) in a "
            "```python block that builds this part.";
        // .strip()
        {
            size_t a = appended.find_first_not_of(" \t\r\n\f\v");
            size_t b = appended.find_last_not_of(" \t\r\n\f\v");
            appended = (a == std::string::npos) ? "" : appended.substr(a, b - a + 1);
        }
        st.current_step = appended;
        AgentCall ac =
            agent_call(use_implement_provider(), st, "modeler", prompts_dir, implement_budget,
                       implement_vision, implement_ctx_cap);
        StageOutcome o;
        fill_meta(o, ac);
        o.raw = ac.reply;
        std::optional<std::string> code = extract_script(ac.reply);
        if (!code) {
            o.verdict = "fail";
            o.reason = "escape hatch: no script";
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = "no script";
            o.has_error = true;
            o.error = "model-escape: no script";
            return o;
        }
        std::string name = "Script";
        if (st.spec.is_object() && st.spec.contains("object") && st.spec["object"].is_string() &&
            !st.spec["object"].get<std::string>().empty())
            name = st.spec["object"].get<std::string>();
        json op = script_escape_op(*code, name);
        json ops = json::array({op});
        json dry = doc::doc_dry_run_ops(doc_id, ops);
        if (!dry.value("ok", false)) {
            o.verdict = "fail";
            o.reason = "escape script invalid: " + dry.value("error", std::string());
            o.parsed_ok = true;
            o.has_ops_count = true;
            o.ops_count = 1;
            o.has_error = true;
            o.error = "model-escape-validate: " + dry.value("error", std::string());
            return o;
        }
        st.last_errors = apply_ops(st, ops);
        o.verdict = "pass";
        o.reason = "escape hatch: script feature applied";
        o.parsed_ok = true;
        o.has_ops_count = true;
        o.ops_count = 1;
        return o;
    };

    // ---- MODEL ----
    actions.model = [&](DesignState& st, int attempt) -> StageOutcome {
        st.last_no_progress = false;
        if (attempt > kEscapeAfter) return model_escape(st);
        AgentCall ac =
            agent_call(use_implement_provider(), st, "modeler", prompts_dir, implement_budget,
                       implement_vision, implement_ctx_cap, cad_seq_directive);
        StageOutcome o;
        fill_meta(o, ac);
        o.raw = ac.reply;
        if (ac.has_call_error) {
            // The provider itself failed (HTTP error, empty/truncated completion). Report the
            // real cause so the run doesn't masquerade it as a "no JSON ops" parse failure.
            o.verdict = "retry";
            o.reason = "provider error: " + ac.call_error;
            o.has_error = true;
            o.error = "model: " + ac.call_error;
            return o;
        }
        OpsResult ores = extract_ops(ac.reply);
        if (!ores.ops) {
            std::string perr = ores.error.value_or("");
            o.verdict = "retry";
            o.reason = "no ops: " + perr;
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = perr;
            o.has_error = true;
            o.error = "model: " + perr;
            return o;
        }
        const json& ops = *ores.ops;
        if (ops.empty()) {
            st.last_no_progress = true;
            o.verdict = "pass";
            o.reason = "no ops needed for step";
            o.parsed_ok = true;
            o.has_ops_count = true;
            o.ops_count = 0;
            o.no_progress = true;
            return o;
        }
        json dry = doc::doc_dry_run_ops(doc_id, ops);
        if (!dry.value("ok", false)) {
            std::string verr = dry.value("error", std::string());
            st.last_errors = {"invalid ops: " + verr};
            o.verdict = "retry";
            o.reason = "ops failed validation";
            o.parsed_ok = true;
            o.has_ops_count = true;
            o.ops_count = static_cast<int>(ops.size());
            o.has_error = true;
            o.error = "model-validate: " + verr;
            return o;
        }
        st.last_errors = apply_ops(st, ops);
        o.verdict = "pass";
        o.reason = "applied " + std::to_string(ops.size()) + " ops";
        o.parsed_ok = true;
        o.has_ops_count = true;
        o.ops_count = static_cast<int>(ops.size());
        return o;
    };

    // ---- REVIEW (stats-only; vision rendering DEFERRED to host P55) ----
    actions.review = [&](DesignState& st, int) -> StageOutcome {
        // Vision review: rasterize the current revision so a vision-capable model can SEE its
        // model and self-correct (fail-soft — text-only review if the render is unavailable).
        st.last_render = nullptr;
        if (review_vision) {
            std::filesystem::path png =
                run_dir(run_id) / ("review_r" + std::to_string(st.revision) + ".png");
            json rr = doc::doc_render_png(doc_id, png.string());
            if (rr.value("ok", false)) st.last_render = png.string();
        }
        BuiltContext bc = build_context(st, "reviewer", prompts_dir, review_ctx_cap);
        auto pr = load_prompt("reviewer", prompts_dir);
        StageOutcome o;
        o.prompt_file = pr.second;
        o.prompt_hash = prompt_hash(bc.system);  // reviewer system is NOT role-prefixed here,
        // mirroring run.py review() which hashes the bare build_context system (not "ROLE:..").
        o.context_chars = bc.context_chars;
        o.context_tokens_est = bc.context_tokens_est;
        o.inputs_digest = inputs_digest(st);
        json review_parts = json::array({text_part(bc.body)});
        if (review_vision && st.last_render.is_string() && !st.last_render.get<std::string>().empty())
            review_parts.push_back(image_part(st.last_render.get<std::string>()));
        json messages = json::array({user_message(review_parts)});
        auto t0 = std::chrono::steady_clock::now();
        ProvReply rep = review_provider(bc.system, messages, review_budget);  // review-scaled budget
        auto t1 = std::chrono::steady_clock::now();
        o.duration_ms = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
        o.thinking = rep.thinking;
        o.model = rep.model;
        std::string reply = rep.has_error ? std::string() : rep.text;
        o.raw = reply;
        ojson verdict = extract_json_obj(reply);
        if (verdict.is_null()) {
            o.verdict = "retry";
            o.reason = "review verdict not parseable";
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = "no verdict json";
            return o;
        }
        bool done = verdict.contains("done") && truthy_json(verdict["done"]);
        std::vector<std::string> issues;
        if (verdict.contains("issues") && verdict["issues"].is_array())
            for (const auto& i : verdict["issues"])
                if (i.is_string() && !i.get<std::string>().empty())
                    issues.push_back(i.get<std::string>());
        st.last_issues = issues;
        if (done || issues.empty()) {
            o.verdict = "pass";
            o.reason = "review: done";
            o.parsed_ok = true;
            o.done = true;
        } else {
            o.verdict = "retry";
            o.reason = "review: " + std::to_string(issues.size()) + " issue(s)";
            o.parsed_ok = true;
            o.done = false;
        }
        return o;
    };

    // ---- FIX ----
    actions.fix = [&](DesignState& st, int) -> StageOutcome {
        // Fix is an implementation stage: when prewalk is armed and there is no separate
        // vision-review provider, use the cheap implement provider (one-way switch notice).
        const bool use_prewalk_fix = prewalk_armed && !review_provider_specialized;
        AgentCall ac =
            use_prewalk_fix
                ? agent_call(use_implement_provider(), st, "fixer", prompts_dir, implement_budget,
                             implement_vision, implement_ctx_cap)
                : agent_call(review_provider, st, "fixer", prompts_dir, review_budget, review_vision,
                             review_ctx_cap);
        StageOutcome o;
        fill_meta(o, ac);
        o.raw = ac.reply;
        if (extract_revert(ac.reply)) {
            try {
                json resp = doc::doc_undo(doc_id);
                st.update_revision(resp.value("revision", st.revision));
                st.update_tree(
                    tree_to_ojson(resp.contains("tree") ? resp["tree"] : json::object()));
                o.verdict = "pass";
                o.reason = "fix: reverted last change";
                o.parsed_ok = true;
            } catch (const std::exception& e) {
                o.verdict = "fail";
                o.reason = std::string("revert failed: ") + e.what();
                o.has_error = true;
                o.error = std::string("fix-revert: ") + e.what();
            }
            return o;
        }
        OpsResult ores = extract_ops(ac.reply);
        if (!ores.ops) {
            std::string perr = ores.error.value_or("");
            o.verdict = "retry";
            o.reason = "fix: no ops (" + perr + ")";
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = perr;
            o.has_error = true;
            o.error = "fix: " + perr;
            return o;
        }
        const json& ops = *ores.ops;
        if (ops.empty()) {
            o.verdict = "pass";
            o.reason = "fix: no change";
            o.parsed_ok = true;
            o.has_ops_count = true;
            o.ops_count = 0;
            return o;
        }
        json dry = doc::doc_dry_run_ops(doc_id, ops);
        if (!dry.value("ok", false)) {
            std::string verr = dry.value("error", std::string());
            st.last_errors = {"invalid fix ops: " + verr};
            o.verdict = "retry";
            o.reason = "fix ops failed validation";
            o.parsed_ok = true;
            o.has_ops_count = true;
            o.ops_count = static_cast<int>(ops.size());
            o.has_error = true;
            o.error = "fix-validate: " + verr;
            return o;
        }
        st.last_errors = apply_ops(st, ops);
        st.last_issues.clear();
        o.verdict = "pass";
        o.reason = "fix: applied " + std::to_string(ops.size()) + " ops";
        o.parsed_ok = true;
        o.has_ops_count = true;
        o.ops_count = static_cast<int>(ops.size());
        return o;
    };

    // ---- MEMORY ----
    actions.memory = [&](DesignState& st, int) -> StageOutcome {
        AgentCall ac = agent_call(provider, st, "memory", prompts_dir, call_budget, vision, ctx_cap);
        StageOutcome o;
        fill_meta(o, ac);
        o.raw = ac.reply;
        ojson mem = extract_json_obj(ac.reply);
        if (mem.is_null()) {
            o.verdict = "pass";
            o.reason = "memory: nothing recorded";
            o.parsed_ok = false;
            o.has_parse_error = true;
            o.parse_error = "no memory json";
            return o;
        }
        store_memory(run_id, mem);
        st.memory = mem;
        o.verdict = "pass";
        o.reason = "memory recorded";
        o.parsed_ok = true;
        return o;
    };

    // ---- plan_steps ----
    actions.plan_steps = [&](DesignState& st) -> std::vector<std::string> {
        std::vector<std::string> steps;
        if (st.plan.is_null() || !st.plan.contains("parts") || !st.plan["parts"].is_array() ||
            st.plan["parts"].empty()) {
            steps.push_back(default_step(st));
            return steps;
        }
        // Decompose each part into an ORDERED task list: a base-solid step, then one step per
        // feature. Smaller modeler calls model far more reliably than one giant "build everything"
        // call (less truncation, and each step's failure is isolated + re-promptable), and the
        // step list is exactly the long-horizon task checklist the UI surfaces. The modeler is
        // told to emit ONLY ops for the current step and not re-emit existing features.
        auto rstrip = [](std::string s) {
            size_t b = s.find_last_not_of(" \t\r\n\f\v");
            return (b == std::string::npos) ? std::string() : s.substr(0, b + 1);
        };
        for (const auto& p : st.plan["parts"]) {
            std::string name = (p.contains("name") && p["name"].is_string())
                                   ? p["name"].get<std::string>()
                                   : "part";
            std::string purpose = (p.contains("purpose") && p["purpose"].is_string())
                                      ? p["purpose"].get<std::string>()
                                      : "";
            std::string dim_s;
            if (p.contains("dimensions") && p["dimensions"].is_object()) {
                bool first = true;
                for (auto it = p["dimensions"].begin(); it != p["dimensions"].end(); ++it) {
                    if (!first) dim_s += ", ";
                    first = false;
                    dim_s += it.key() + "=" + py_scalar_str(it.value());
                }
            } else if (p.contains("dimensions")) {
                dim_s = py_scalar_str(p["dimensions"]);
            }
            // Placement: position this part in the shared assembly space so multiple parts fit
            // together instead of stacking at the origin. The modeler places primitives with `at`,
            // and sketched bodies by building the sketch at those coordinates / on an offset plane.
            std::string place_s;
            if (p.contains("placement") && p["placement"].is_object() &&
                p["placement"].contains("pos") && p["placement"]["pos"].is_array() &&
                p["placement"]["pos"].size() >= 3) {
                const auto& pos = p["placement"]["pos"];
                auto num = [&](const ojson& v) {
                    return v.is_number() ? py_scalar_str(v) : std::string("0");
                };
                std::string xyz = num(pos[0]) + "," + num(pos[1]) + "," + num(pos[2]);
                if (xyz != "0,0,0")
                    place_s = " Position this part's origin at [" + xyz +
                              "] in the shared assembly space (primitives: use \"at\":[" + xyz +
                              "]; sketched bodies: build the sketch at those coordinates or on an "
                              "offset plane) so it does not overlap the other parts.";
            }
            steps.push_back(rstrip("Build the base solid of part '" + name + "' (" + purpose +
                                   "). Dimensions: " + dim_s + "." + place_s));
            if (p.contains("features") && p["features"].is_array())
                for (const auto& f : p["features"]) {
                    std::string fs = py_scalar_str(f);
                    if (rstrip(fs).empty()) continue;
                    steps.push_back(rstrip("Refine part '" + name + "': " + fs));
                }
        }
        return steps;
    };

    // ----------------------------------------------------------------- drive
    const auto t_run0 = std::chrono::steady_clock::now();
    json result = run_machine(state, actions, events, options.value("budgets", json::object()));

    // revision = session.revision (re-read live; matches run.py getattr(session,"revision")).
    json tree_now = doc::doc_tree(doc_id);
    int revision = tree_now.value("revision", state.revision);

    // T-H1 telemetry (contracts/telemetry.md): the design run's output is one AI
    // artifact; the run applies its ops to the live document as it goes, so a
    // completed run IS the apply — "applied" is inferred from completion
    // (buttonless v1; the GUI takes the result over the C-ABI bridge and has no
    // core/agent linkage, so the emission lives here — see work/questions/T-H1.md).
    {
        std::string consent = consent_tier(load_config());
        if (offline) consent = "off";  // options-level offline flag: same hard override
        const int64_t run_latency_ms =
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - t_run0).count();
        const json model_info = {{"provider", provider_cfg.value("name", std::string())},
                                 {"model", provider_cfg.value("model", std::string())},
                                 {"adapter", nullptr}};
        // One artifact per design run; sidecar refs stay empty at run granularity
        // (per-attempt raws are already in attempt_<stage>_<n>.txt via agent_output).
        emit_ai_artifact(events, /*artifact_id=*/run_id, /*insertion_point=*/"IP-1",
                         model_info, /*prompt_hash=*/"",
                         /*input_ref=*/lp_state.last_input_ref,
                         /*output_ref=*/lp_state.last_output_ref,
                         /*base_revision=*/tree0.value("revision", 0),
                         /*verifier_scores=*/json::object(), run_latency_ms, consent);
        if (result.value("status", std::string()) == "done")
            emit_ai_artifact_action(events, /*artifact_id=*/run_id, "applied", revision,
                                    /*edit_ops_ref=*/"", /*undo_within_ops=*/0,
                                    /*dwell_ms=*/0, /*note=*/"", consent);
    }

    {
        json fin = json{{"status", result["status"]},
                       {"stop_reason", result["stop_reason"]},
                       {"revision", revision}};
        // Surface provider HTTP detail on the phase card (never "budget exhausted").
        if (result.contains("error_detail") && result["error_detail"].is_string() &&
            !result["error_detail"].get<std::string>().empty())
            fin["error_detail"] = result["error_detail"];
        if (result.contains("error_human") && result["error_human"].is_string() &&
            !result["error_human"].get<std::string>().empty())
            fin["error_human"] = result["error_human"];
        events.emit("run_finished", fin);
    }

    json out = json{{"run_id", run_id},
                    {"status", result["status"]},
                    {"stop_reason", result["stop_reason"]},
                    {"revision", revision},
                    {"doc_id", state.doc_id}};
    if (result.contains("error_detail") && result["error_detail"].is_string() &&
        !result["error_detail"].get<std::string>().empty())
        out["error_detail"] = result["error_detail"];
    if (result.contains("error_human") && result["error_human"].is_string() &&
        !result["error_human"].get<std::string>().empty())
        out["error_human"] = result["error_human"];
    return out;
}

json run_assembly_design(const std::string& request, const std::string& doc_id,
                         const json& provider_cfg, EmitFn emit, const json& options) {
    std::string prompts_dir = options.value("prompts_dir", std::string());
    std::string run_id = options.value("run_id", std::string());
    if (run_id.empty())
        run_id = "asm-" + std::to_string(static_cast<unsigned long long>(
                              std::chrono::steady_clock::now().time_since_epoch().count()));

    // 1) Plan the request to learn the parts + whether it's an assembly. One intake + one planner
    //    call (cheap relative to a full build); the per-part run_design re-plans its own part.
    ProviderFn provider = make_provider(provider_cfg);
    const CallBudget cb = fit_call_budget(provider_cfg, call_budget_for(provider_cfg));
    const int budget = cb.output;
    const int ctx_cap = cb.input_cap;
    DesignState pstate;
    pstate.request = request;
    pstate.doc_id = doc_id;
    {
        AgentCall ia = agent_call(provider, pstate, "intake", prompts_dir, budget, false, ctx_cap);
        ojson spec = extract_json_obj(ia.reply);
        if (!spec.is_null()) pstate.spec = spec;
    }
    AgentCall pa = agent_call(provider, pstate, "planner", prompts_dir, budget, false, ctx_cap);
    ojson plan = extract_json_obj(pa.reply);
    bool want_asm = !plan.is_null() && plan.is_object() && truthy_json(plan.value("assembly", ojson(false)));
    ojson parts = (!plan.is_null() && plan.is_object() && plan.contains("parts") &&
                   plan["parts"].is_array())
                      ? plan["parts"]
                      : ojson::array();

    // Not an assembly (or <2 parts): the existing single-doc multibody path is correct.
    if (!want_asm || parts.size() < 2)
        return run_design(request, doc_id, provider_cfg, emit, options);

    RunEvents events(run_id, emit);
    events.emit("run_started", json{{"request", request},
                                    {"doc_id", doc_id},
                                    {"assembly", true},
                                    {"part_count", static_cast<int>(parts.size())}});

    // 2) Build each part in its OWN child doc (full run_design), then save it to a .step+ file.
    struct SavedPart { std::string path, name; double pos[3]; double rot[3]; };
    std::vector<SavedPart> saved;
    int idx = 0;
    for (const auto& p : parts) {
        std::string name = (p.contains("name") && p["name"].is_string())
                               ? p["name"].get<std::string>() : ("part" + std::to_string(idx + 1));
        std::string purpose = (p.contains("purpose") && p["purpose"].is_string())
                                  ? p["purpose"].get<std::string>() : "";
        std::string dim_s;
        if (p.contains("dimensions") && p["dimensions"].is_object())
            for (auto it = p["dimensions"].begin(); it != p["dimensions"].end(); ++it) {
                if (!dim_s.empty()) dim_s += ", ";
                dim_s += it.key() + "=" + py_scalar_str(it.value());
            }
        std::string feats;
        if (p.contains("features") && p["features"].is_array())
            for (const auto& f : p["features"]) {
                if (!feats.empty()) feats += "; ";
                feats += py_scalar_str(f);
            }
        // Placement: position (mm) + rotation (Euler degrees about X, then Y, then Z). Rotation
        // lets a control surface be assembled DEFLECTED (e.g. an aileron rotated about its hinge
        // axis) so a downstream CFD run sees the deflected configuration — the planner emits
        // placement.rot per part. A missing rot is no rotation (identity), the common case.
        double pos[3] = {0, 0, 0};
        double rot[3] = {0, 0, 0};
        if (p.contains("placement") && p["placement"].is_object()) {
            const json& pl = p["placement"];
            if (pl.contains("pos") && pl["pos"].is_array() && pl["pos"].size() >= 3)
                for (int k = 0; k < 3; ++k)
                    if (pl["pos"][k].is_number()) pos[k] = pl["pos"][k].get<double>();
            if (pl.contains("rot") && pl["rot"].is_array() && pl["rot"].size() >= 3)
                for (int k = 0; k < 3; ++k)
                    if (pl["rot"][k].is_number()) rot[k] = pl["rot"][k].get<double>();
        }

        events.emit("part_started", json{{"index", idx},
                                         {"total", static_cast<int>(parts.size())},
                                         {"name", name}});

        std::string part_req = "Build ONLY this single standalone part, centered at the origin (it "
                               "will be positioned in an assembly afterward). Part: '" + name +
                               "' — " + purpose + ". Dimensions: " + dim_s +
                               ". Features: " + feats;
        std::string child;
        try {
            json nd = doc::doc_new("part", name);
            child = jstr(nd, "doc_id");
        } catch (const std::exception& e) {
            events.emit("part_skipped",
                        json{{"index", idx}, {"name", name}, {"reason", e.what()}});
        } catch (...) {
            events.emit("part_skipped",
                        json{{"index", idx}, {"name", name}, {"reason", "doc_new failed"}});
        }
        if (child.empty()) { ++idx; continue; }

        json sub_opts = options;
        sub_opts["run_id"] = run_id + "-p" + std::to_string(idx);
        run_design(part_req, child, provider_cfg, emit, sub_opts);

        std::filesystem::path path = run_dir(run_id) / ("part_" + std::to_string(idx) + ".step+");
        try {
            doc::doc_save(child, path.string());
            SavedPart sp;
            sp.path = std::filesystem::absolute(path).string();
            sp.name = name;
            sp.pos[0] = pos[0]; sp.pos[1] = pos[1]; sp.pos[2] = pos[2];
            sp.rot[0] = rot[0]; sp.rot[1] = rot[1]; sp.rot[2] = rot[2];
            saved.push_back(sp);
        } catch (const std::exception& e) {
            events.emit("part_skipped",
                        json{{"index", idx}, {"name", name}, {"reason", e.what()}});
        } catch (...) {
            events.emit("part_skipped",
                        json{{"index", idx}, {"name", name}, {"reason", "doc_save failed"}});
        }
        ++idx;
    }

    if (saved.empty()) {
        events.emit("run_finished", json{{"status", "failed"},
                                         {"stop_reason", "no_parts_built"}, {"revision", 0}});
        return json{{"run_id", run_id}, {"status", "failed"}, {"stop_reason", "no_parts_built"},
                    {"revision", 0}, {"doc_id", doc_id}};
    }

    // 3) Create the assembly doc and insert each saved part as a positioned, fixed component.
    std::string asm_name = (!plan.is_null() && plan.contains("name") && plan["name"].is_string())
                               ? plan["name"].get<std::string>() : "Assembly";
    std::string asm_id;
    try {
        json nd = doc::doc_new("assembly", asm_name);
        asm_id = jstr(nd, "doc_id");
    } catch (const std::exception& e) {
        events.emit("assembly_failed", json{{"reason", e.what()}});
    } catch (...) {
        events.emit("assembly_failed", json{{"reason", "doc_new failed"}});
    }
    if (asm_id.empty()) {
        events.emit("run_finished", json{{"status", "failed"},
                                         {"stop_reason", "assembly_doc_failed"}, {"revision", 0}});
        return json{{"run_id", run_id}, {"status", "failed"}, {"stop_reason", "assembly_doc_failed"},
                    {"revision", 0}, {"doc_id", doc_id}};
    }
    // 4x4 row-major component transform = R(rot)·... + translation. Rotation is Euler degrees
    // applied X then Y then Z (R = Rz·Ry·Rx), matching the placement.rot the planner emits.
    auto placement_transform = [](const double pos[3], const double rot[3]) {
        const double d2r = 3.14159265358979323846 / 180.0;
        double cx = std::cos(rot[0] * d2r), sx = std::sin(rot[0] * d2r);
        double cy = std::cos(rot[1] * d2r), sy = std::sin(rot[1] * d2r);
        double cz = std::cos(rot[2] * d2r), sz = std::sin(rot[2] * d2r);
        // R = Rz * Ry * Rx (row-major 3x3).
        double r00 = cz * cy, r01 = cz * sy * sx - sz * cx, r02 = cz * sy * cx + sz * sx;
        double r10 = sz * cy, r11 = sz * sy * sx + cz * cx, r12 = sz * sy * cx - cz * sx;
        double r20 = -sy,     r21 = cy * sx,                r22 = cy * cx;
        return json{r00, r01, r02, pos[0],
                    r10, r11, r12, pos[1],
                    r20, r21, r22, pos[2],
                    0,   0,   0,   1};
    };
    json comp_ops = json::array();
    for (size_t k = 0; k < saved.size(); ++k) {
        const SavedPart& sp = saved[k];
        json transform = placement_transform(sp.pos, sp.rot);
        comp_ops.push_back(json{{"op", "asm_insert_component"},
                                {"source", {{"path", sp.path}}},
                                {"name", sp.name},
                                {"transform", transform},
                                {"fixed", k == 0}});
    }
    int asm_rev = 0;
    // Absolute path the host opens to display the assembly. The C++ core REJECTS opening
    // assembly docs (doc_open "unsupported" by schema), so the host routes this file open to
    // the Python worker — exactly the File>Open / Explorer-double-click path. Components are
    // referenced by absolute .step+ paths (saved above under the same run_dir), so the loaded
    // assembly resolves its parts regardless of the host's cwd.
    std::string asm_path =
        std::filesystem::absolute(run_dir(run_id) / (asm_name + ".step+")).string();
    try {
        json before = doc::doc_tree(asm_id);
        json resp = doc::doc_apply_ops(asm_id, before.value("revision", 0), comp_ops, false);
        asm_rev = resp.value("revision", 0);
        doc::doc_save(asm_id, asm_path);
    } catch (const std::exception& e) {
        events.emit("run_finished", json{{"status", "failed"},
                                         {"stop_reason", std::string("assemble_failed: ") + e.what()},
                                         {"revision", 0}});
        return json{{"run_id", run_id}, {"status", "failed"},
                    {"stop_reason", "assemble_failed"}, {"revision", 0}, {"doc_id", asm_id},
                    {"created_doc", true}, {"assembly", true}};
    }

    events.emit("run_finished", json{{"status", "done"},
                                     {"stop_reason", "assembled"},
                                     {"revision", asm_rev},
                                     {"components", static_cast<int>(saved.size())}});
    return json{{"run_id", run_id}, {"status", "done"}, {"stop_reason", "assembled"},
                {"revision", asm_rev}, {"doc_id", asm_id}, {"created_doc", true},
                {"assembly", true}, {"asm_path", asm_path},
                {"components", static_cast<int>(saved.size())}};
}

// ============================================================================ mission
// The closed set of capabilities the mission planner may emit. design/assembly both map
// to run_assembly_design (it auto-detects assembly from its own planner); the rest map to
// the noted backends. Anything outside this set is skipped (forward-compatible).
namespace {

// One human + deterministic line per flow result, plus a qualitative read. Kept honest:
// the LBM grid is coarse (the result string itself says "directional estimate"), so the
// verdict reports the L/D ratio and flags clearly-bad signs rather than over-claiming.
std::string deterministic_verdict(const ojson& sim_results) {
    if (!sim_results.is_array() || sim_results.empty())
        return "No simulation was run, so there is nothing to judge yet.";
    std::ostringstream os;
    int n = 0;
    for (const auto& sr : sim_results) {
        if (!sr.is_object() || !truthy_json(sr.value("ok", ojson(false)))) {
            os << "- A flow run did not produce usable forces"
               << (sr.contains("answer") ? (": " + sr.value("answer", std::string())) : ".")
               << "\n";
            continue;
        }
        double drag = sr.value("drag_n", 0.0);
        double lift = sr.value("lift_n", 0.0);
        double cd = sr.value("cd", 0.0);
        double v = sr.value("velocity_ms", 0.0);
        double aoa = sr.value("aoa_deg", 0.0);
        double ld = lift / std::max(std::fabs(drag), 1e-9);
        std::ostringstream line;
        line.precision(2);
        line << std::fixed;
        line << "- At " << v << " m/s, " << aoa << " deg AoA: lift " << lift << " N, drag "
             << drag << " N (Cd " << cd << "), lift-to-drag ratio " << ld << ". ";
        if (lift <= 0.0)
            line << "Lift is non-positive — this orientation does not generate useful lift; "
                    "check the angle of attack or the wing.";
        else if (ld >= 10.0)
            line << "That is an efficient L/D for a model aircraft — the design looks "
                    "aerodynamically sound.";
        else if (ld >= 4.0)
            line << "That is a workable L/D — the design flies, with room to cut drag.";
        else
            line << "That L/D is low — the shape is draggy for the lift it makes; consider a "
                    "cleaner profile or higher aspect ratio.";
        os << line.str() << "\n";
        ++n;
    }
    if (n == 0) os << "No flow run produced usable forces.\n";
    os << "\n(LBM is a coarse, directional estimate — trust the trend and the L/D, not the "
          "absolute drag to three digits.)";
    return os.str();
}

// Build the verdict text: deterministic baseline always, then (best-effort) an LLM reporter
// pass layered on top. Falls back to the deterministic text when offline or on any error.
std::string build_verdict(const ProviderFn& provider, const std::string& prompts_dir, int budget,
                          int ctx_cap, const std::string& mission_request,
                          const std::string& design_summary, const ojson& sim_results) {
    std::string deterministic = deterministic_verdict(sim_results);
    // Compose the reporter's context into the request field (build_context surfaces request).
    DesignState rstate;
    rstate.request = "Mission: " + mission_request + "\n\nWhat was built:\n" +
                     (design_summary.empty() ? "(model summary unavailable)" : design_summary) +
                     "\n\nMeasured results:\n" + deterministic;
    AgentCall ra = agent_call(provider, rstate, "reporter", prompts_dir, budget, false, ctx_cap);
    if (ra.has_call_error || ra.reply.empty()) return deterministic;
    // Trim whitespace.
    size_t a = ra.reply.find_first_not_of(" \t\r\n");
    size_t b = ra.reply.find_last_not_of(" \t\r\n");
    if (a == std::string::npos) return deterministic;
    return ra.reply.substr(a, b - a + 1);
}

// A compact digest of the doc's feature tree, for the reporter's "what was built" context.
std::string doc_design_summary(const std::string& doc_id) {
    try {
        json t = doc::doc_tree(doc_id);
        if (t.is_object() && t.contains("tree"))
            return digest_tree(tree_to_ojson(t["tree"]), 20);
    } catch (...) {
    }
    return "";
}

}  // namespace

json run_mission(const std::string& request, const std::string& doc_id, const json& provider_cfg,
                 EmitFn emit, SimFn sim, const json& options) {
    std::string prompts_dir = options.value("prompts_dir", std::string());
    std::string run_id = options.value("run_id", std::string());
    if (run_id.empty())
        run_id = "mission-" + std::to_string(static_cast<unsigned long long>(
                                  std::chrono::steady_clock::now().time_since_epoch().count()));

    ProviderFn provider = make_provider(provider_cfg);
    const CallBudget cb = fit_call_budget(provider_cfg, call_budget_for(provider_cfg));
    const int budget = cb.output;
    const int ctx_cap = cb.input_cap;
    RunEvents events(run_id, emit);

    // 1) Decompose the request into an ordered task list. Normally this costs one
    //    mission_planner call; on any parse failure / offline provider it degrades to a
    //    single design task, so a plain "make a bracket" still builds exactly as before.
    //    E4 (AI-experience plan): an explicit options.capability lets a caller (the in-app
    //    Copilot "Reverse-engineer import" chip) route straight to one capability without
    //    depending on the mission_planner prompt recognizing wording it was never taught
    //    about (that prompt template is outside this node's write-set) -- deterministic
    //    instead of hoping an LLM infers "reverse_engineer" from prose.
    struct Task {
        std::string capability, title, req, target;
    };
    static const std::set<std::string> kCaps = {
        "design",  "assembly", "simulate_flow", "simulate_fea",
        "drawing", "evaluate", "report",        "reverse_engineer"};
    std::vector<Task> tasks;
    const std::string capability_override = options.value("capability", std::string());
    if (!capability_override.empty() && kCaps.count(capability_override)) {
        tasks.push_back(Task{capability_override, "Reverse-engineer import", request, "active"});
    } else {
        DesignState mstate;
        mstate.request = request;
        mstate.doc_id = doc_id;
        AgentCall ma =
            agent_call(provider, mstate, "mission_planner", prompts_dir, budget, false, ctx_cap);
        ojson plan = extract_json_obj(ma.reply);
        if (!plan.is_null() && plan.is_object() && plan.contains("tasks") &&
            plan["tasks"].is_array()) {
            for (const auto& t : plan["tasks"]) {
                if (!t.is_object()) continue;
                Task tk;
                tk.capability = t.value("capability", std::string());
                tk.title = t.value("title", std::string());
                tk.req = t.value("request", std::string());
                tk.target = t.value("target", std::string("previous"));
                if (tk.capability.empty()) continue;
                if (tk.req.empty()) tk.req = request;  // defensive: hand the modeler something
                tasks.push_back(std::move(tk));
                if (tasks.size() >= 8) break;  // hard cap (planner is asked for <=6)
            }
        }
        if (tasks.empty())
            tasks.push_back(Task{"design", "Build the model", request, "active"});
    }

    json task_list = json::array();
    for (const auto& tk : tasks)
        task_list.push_back(json{{"capability", tk.capability}, {"title", tk.title}});
    events.emit("mission_started", json{{"request", request},
                                        {"task_count", static_cast<int>(tasks.size())},
                                        {"tasks", task_list}});

    // 2) Execute tasks in order, threading the working document forward.
    std::string current_doc = doc_id;  // "previous" target resolves to this
    std::string final_doc = doc_id;
    bool created_doc = false;
    std::string asm_path;
    int components = 0;
    int revision = 0;
    bool any_built = false;  // geometry was produced (drives stop_reason + tab-open)
    bool any_ok = false;     // any task completed (drives the mission's done/failed status)
    bool reported = false;
    ojson sim_results = ojson::array();
    std::string design_summary;
    std::string verdict;
    json re_report = json::object();  // populated by the "reverse_engineer" capability
    json task_results = json::array();

    for (size_t i = 0; i < tasks.size(); ++i) {
        const Task& tk = tasks[i];
        std::string target_doc = (tk.target == "active") ? doc_id : current_doc;
        events.emit("mission_task_started",
                    json{{"index", static_cast<int>(i)},
                         {"total", static_cast<int>(tasks.size())},
                         {"capability", tk.capability},
                         {"title", tk.title},
                         {"doc_id", target_doc}});

        std::string status = "skipped";
        std::string summary;

        if (!kCaps.count(tk.capability)) {
            summary = "unknown capability '" + tk.capability + "'";
        } else if (tk.capability == "design" || tk.capability == "assembly") {
            json sub_opts = options;
            sub_opts["run_id"] = run_id + "-t" + std::to_string(i);
            json rr = run_assembly_design(tk.req, target_doc, provider_cfg, emit, sub_opts);
            status = rr.value("status", std::string("done"));
            summary = rr.value("stop_reason", std::string());
            if (truthy_json(ojson(rr.value("created_doc", false)))) {
                current_doc = rr.value("doc_id", current_doc);
                created_doc = true;
                asm_path = rr.value("asm_path", asm_path);
                components = rr.value("components", components);
            } else {
                current_doc = target_doc;  // design mutated the target doc in place
            }
            final_doc = current_doc;
            revision = rr.value("revision", revision);
            if (status == "done") {
                any_built = true;
                design_summary = doc_design_summary(current_doc);
            }
        } else if (tk.capability == "simulate_flow") {
            if (sim) {
                json sr = sim(current_doc, tk.req, json::object());
                sim_results.push_back(tree_to_ojson(sr));
                bool ok = sr.value("ok", false);
                status = ok ? "done" : "failed";
                summary = sr.value("answer", std::string());
            } else {
                summary = "no flow-sim backend wired";
            }
        } else if (tk.capability == "report") {
            verdict = build_verdict(provider, prompts_dir, budget, ctx_cap, request, design_summary,
                                    sim_results);
            reported = true;
            status = "done";
            summary = verdict;
        } else if (tk.capability == "reverse_engineer") {
            // WS-E E4 (AI-experience plan): keep the import doc (target_doc) as a READ-ONLY
            // reference, build the reconstruction into a fresh doc, then verify with the real
            // doc_eval_compare geometric comparison and surface match_pct in both the task
            // summary and a dedicated event.
            //
            // FALLBACK TAKEN (pre-decided in the plan): full per-stage plumbing would mean
            // threading compare feedback into the reviewer/fixer BuiltContext at EVERY
            // modeler/review/fix iteration inside run_design's ~500-line stage loop -- a loop
            // shared with "design"/"assembly", and whose prompt templates (reviewer.md/
            // fixer.md, under services/agent/prompts/) would need to learn to read an injected
            // compare block. Both are outside this node's write-set and out of proportion for
            // one capability branch. Instead: run the normal design loop once into a new doc
            // (a real, not per-stage, verification pass), compare with the reference via the
            // actual eval_compare RPC (never a canned number), and hand back one explicit
            // follow-up instruction so a second copilot turn can close any residual gap via its
            // normal add_features / eval_compare op path.
            const std::string ref_doc = target_doc;
            std::string new_doc_id;
            try {
                json nd = doc::doc_new("part", "Reverse-Engineered");
                new_doc_id = nd.value("doc_id", std::string());
            } catch (const std::exception& e) {
                summary = std::string("could not create reconstruction doc: ") + e.what();
            } catch (...) {
                summary = "could not create reconstruction doc";
            }
            if (!new_doc_id.empty()) {
                const std::string augmented =
                    tk.req +
                    "\n\nDocument " + ref_doc + " holds the ORIGINAL imported body — it is a "
                    "READ-ONLY reference; never edit it. You are building INTO this new, empty "
                    "document instead. Reproduce the reference geometry as a normal parametric "
                    "feature tree (sketches, extrudes, holes, fillets, patterns, ...), not a "
                    "single dumb import.";
                json sub_opts = options;
                sub_opts["run_id"] = run_id + "-t" + std::to_string(i);
                json rr = run_assembly_design(augmented, new_doc_id, provider_cfg, emit, sub_opts);
                status = rr.value("status", std::string("done"));
                summary = rr.value("stop_reason", std::string());
                current_doc = rr.value("doc_id", new_doc_id);
                final_doc = current_doc;
                revision = rr.value("revision", revision);
                if (status == "done") {
                    any_built = true;
                    design_summary = doc_design_summary(current_doc);
                }
                try {
                    json cmp = doc::doc_eval_compare(json{{"doc_a", current_doc}, {"doc_b", ref_doc}});
                    re_report = json{{"new_doc_id", current_doc}, {"ref_doc_id", ref_doc},
                                     {"compare", cmp}};
                    events.emit("reverse_engineer_compare", re_report);
                    if (cmp.value("degraded", false)) {
                        summary += " | compare degraded (near-identical geometry defeated the "
                                   "boolean); volume_ratio=" +
                                   std::to_string(cmp.value("volume_ratio", 0.0));
                    } else {
                        const double pct = cmp.value("match_pct", 0.0);
                        summary += " | match_pct=" + std::to_string(pct);
                        if (pct < 99.0)
                            summary += " — residual remains; iterate with add_features on " +
                                       current_doc + " and re-check via eval_compare(" +
                                       current_doc + ", " + ref_doc + ").";
                    }
                } catch (const std::exception& e) {
                    summary += std::string(" | compare failed: ") + e.what();
                } catch (...) {
                    summary += " | compare failed";
                }
            }
        } else {
            // simulate_fea / drawing / evaluate — recognized but not yet executed here.
            summary = "capability '" + tk.capability + "' is not yet wired into missions";
        }

        if (status == "done") any_ok = true;
        events.emit("mission_task_result", json{{"index", static_cast<int>(i)},
                                                 {"capability", tk.capability},
                                                 {"status", status},
                                                 {"doc_id", current_doc},
                                                 {"summary", summary}});
        task_results.push_back(json{{"capability", tk.capability},
                                    {"title", tk.title},
                                    {"status", status},
                                    {"doc_id", current_doc},
                                    {"summary", summary}});
    }

    // If the mission ran a sim but the planner forgot an explicit report task, still produce a
    // verdict — that is the whole point of "prove it's a good design".
    if (!reported && sim_results.is_array() && !sim_results.empty())
        verdict = build_verdict(provider, prompts_dir, budget, ctx_cap, request, design_summary,
                                sim_results);

    std::string stop_reason = any_built ? (created_doc ? "assembled" : "built")
                                        : (any_ok ? "done" : "planned");
    std::string status = any_ok ? "done" : "failed";
    events.emit("mission_finished", json{{"status", status},
                                          {"stop_reason", stop_reason},
                                          {"doc_id", final_doc},
                                          {"verdict", verdict}});

    json result = json{{"run_id", run_id},     {"status", status},
                       {"stop_reason", stop_reason}, {"doc_id", final_doc},
                       {"revision", revision},  {"verdict", verdict},
                       {"tasks", task_results}};
    if (created_doc) {
        result["created_doc"] = true;
        result["assembly"] = true;
        if (!asm_path.empty()) result["asm_path"] = asm_path;
        result["components"] = components;
    }
    if (!re_report.empty()) result["reverse_engineer"] = re_report;
    return result;
}

}  // namespace agent
}  // namespace aicad
